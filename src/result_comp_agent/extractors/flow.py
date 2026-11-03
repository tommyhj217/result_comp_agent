"""흐름형 문서 추출기: Word(.docx) · Markdown(.md) · 텍스트(.txt) · PDF(.pdf).

Word
  * 본문 순서대로 문단/표 추출 (콘텐츠 컨트롤 내부 포함)
  * '변경 내용 추적'이 남은 문서는 '모두 승인' 기준 텍스트로 읽음 (삽입 포함, 삭제 제외) → meta 에 표시
  * 제목 스타일(Heading N / 제목 N / Title) → 섹션, 목록 스타일 → list
  * 메모(주석)와 메모가 달린 문단 연결
PDF
  * 페이지별 텍스트 줄 단위 (텍스트 레이어가 없으면 meta 에 경고) — 추출 특성상 서식/배치 정보는 비교하지 않음
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Optional

from ..diff import normalize as N
from ..diff.flow_diff import Block, DocComment, FlowDoc
from ..diff.grid import Grid, GridCell
from .excel import file_sha256

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
_HEADING_RE = re.compile(r"^(heading|제목|title|부제)\s*(\d*)$", re.I)


def _grid_from_rows(rows: list[list[str]], name: str) -> Grid:
    g = Grid(grid_id="", name=name, is_excel=False)
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            t = N.clean_text(v or "")
            cv = N.canon(t)
            if cv is not None:
                g.cells[(r, c)] = GridCell(canon=cv, display=t)
    return g


# ------------------------------------------------------------------------------ Word
def _run_text(r) -> str:
    out = []
    for el in r:  # 직계 자식만 (텍스트 상자 안의 중첩 run 중복 방지)
        tag = el.tag
        if tag == W + "t":
            out.append(el.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
        elif tag == W + "noBreakHyphen":
            out.append("-")
    return "".join(out)


def _p_text(p_el) -> str:
    """문단 텍스트 ('모두 승인' 관점: w:ins 포함, w:del(delText) 제외, 필드코드 instrText 제외)."""
    parts = []
    for r in p_el.iter(W + "r"):
        # 삭제된 run 은 w:del 아래에 있으며 w:delText 를 쓰므로 w:t 가 없다 → 자연히 제외
        parts.append(_run_text(r))
    return N.clean_text("".join(parts))


def _p_style(p_el, styles: dict[str, str]) -> tuple[str, int, Optional[str]]:
    ppr = p_el.find(W + "pPr")
    sid = None
    lvl_outline = None
    is_list = False
    if ppr is not None:
        ps = ppr.find(W + "pStyle")
        if ps is not None:
            sid = ps.get(W + "val")
        ol = ppr.find(W + "outlineLvl")
        if ol is not None:
            try:
                lvl_outline = int(ol.get(W + "val")) + 1
            except (TypeError, ValueError):
                pass
        is_list = ppr.find(W + "numPr") is not None
    name = styles.get(sid or "", sid or "")
    m = _HEADING_RE.match((name or "").strip()) or _HEADING_RE.match((sid or "").strip())
    if m:
        lvl = int(m.group(2)) if m.group(2) else (0 if m.group(1).lower() in ("title", "제목") and not m.group(2) else 1)
        return "heading", lvl, name
    if lvl_outline is not None and lvl_outline <= 9:
        return "heading", lvl_outline, name
    if is_list or (name or "").lower().startswith(("list", "목록")):
        return "list", 0, name
    return "para", 0, name


def extract_docx(path: str | Path) -> FlowDoc:
    import docx  # python-docx

    path = str(path)
    d = docx.Document(path)
    styles = {}
    try:
        for st in d.styles:
            if st.style_id:
                styles[st.style_id] = st.name or st.style_id
    except Exception:  # pragma: no cover
        pass
    doc = FlowDoc(path=path, sha256=file_sha256(path))
    body = d.element.body
    comment_anchor: dict[str, int] = {}
    tracked = bool(body.findall(".//" + W + "ins")) or bool(body.findall(".//" + W + "del"))

    def walk(parent):
        for el in parent.iterchildren():
            tag = el.tag
            if tag == W + "p":
                text = _p_text(el)
                for ref in el.iter(W + "commentReference"):
                    cid = ref.get(W + "id")
                    if cid is not None:
                        comment_anchor.setdefault(cid, len(doc.blocks) if text.strip() else max(0, len(doc.blocks) - 1))
                for rs in el.iter(W + "commentRangeStart"):
                    cid = rs.get(W + "id")
                    if cid is not None and text.strip():
                        comment_anchor.setdefault(cid, len(doc.blocks))
                if not text.strip():
                    continue
                kind, lvl, sname = _p_style(el, styles)
                doc.blocks.append(Block(kind=kind, text=text.strip(), level=lvl, style=sname))
            elif tag == W + "tbl":
                rows = []
                for tr in el.iterchildren(W + "tr"):
                    row = []
                    for tc in tr.iterchildren(W + "tc"):
                        row.append("\n".join(t for t in (_p_text(p).strip() for p in tc.iter(W + "p")) if t))
                    rows.append(row)
                g = _grid_from_rows(rows, f"표{sum(1 for b in doc.blocks if b.is_table) + 1}")
                if g.cells:
                    doc.blocks.append(Block(kind="table", grid=g))
            elif tag == W + "sdt":
                content = el.find(W + "sdtContent")
                if content is not None:
                    walk(content)

    walk(body)
    try:
        for c in d.comments:
            cid = str(c.comment_id)
            doc.comments.append(DocComment(text=N.clean_text(c.text or "").strip(), author=c.author or "", block_index=comment_anchor.get(cid)))
    except Exception:  # 메모 파트가 없거나 구버전 python-docx
        pass
    doc.meta = {"tracked_changes": tracked, "paragraphs": sum(1 for b in doc.blocks if not b.is_table), "tables": sum(1 for b in doc.blocks if b.is_table)}
    if tracked:
        doc.meta["note"] = "변경 내용 추적(수정 표시)이 남아 있어 '모두 승인' 기준으로 비교함"
    return doc


# ------------------------------------------------------------------------------ Markdown / Text
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _read_text_file(path: str) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding
    except Exception:  # pragma: no cover
        pass
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def _split_md_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def extract_markdown(path: str | Path) -> FlowDoc:
    path = str(path)
    text, enc = _read_text_file(path)
    lines = N.clean_text(text).split("\n")
    doc = FlowDoc(path=path, sha256=file_sha256(path), meta={"encoding": enc})
    para: list[str] = []

    def flush():
        if para:
            doc.blocks.append(Block(kind="para", text=" ".join(x.strip() for x in para).strip()))
            para.clear()

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            flush()
            fence = ln.strip()[:3]
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            doc.blocks.append(Block(kind="code", text="\n".join(body).strip("\n")))
            continue
        if "|" in ln and i + 1 < len(lines) and _MD_TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [_split_md_row(ln)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_md_row(lines[i]))
                i += 1
            doc.blocks.append(Block(kind="table", grid=_grid_from_rows(rows, f"표{sum(1 for b in doc.blocks if b.is_table) + 1}")))
            continue
        m = _MD_HEADING.match(ln)
        if m:
            flush()
            doc.blocks.append(Block(kind="heading", text=m.group(2).strip(), level=len(m.group(1))))
        elif _MD_LIST.match(ln):
            flush()
            lm = _MD_LIST.match(ln)
            doc.blocks.append(Block(kind="list", text=lm.group(3).strip(), level=len(lm.group(1).expandtabs(2)) // 2))
        elif not ln.strip():
            flush()
        else:
            para.append(ln)
        i += 1
    flush()
    return doc


def extract_text(path: str | Path) -> FlowDoc:
    """일반 텍스트: 비어 있지 않은 줄 단위 블록 (회의록·로그 등에 적합)."""
    path = str(path)
    text, enc = _read_text_file(path)
    doc = FlowDoc(path=path, sha256=file_sha256(path), meta={"encoding": enc})
    for ln in N.clean_text(text).split("\n"):
        if ln.strip():
            doc.blocks.append(Block(kind="para", text=ln.rstrip()))
    return doc


# ------------------------------------------------------------------------------ PDF
def extract_pdf(path: str | Path) -> FlowDoc:
    from pypdf import PdfReader

    path = str(path)
    reader = PdfReader(path)
    doc = FlowDoc(path=path, sha256=file_sha256(path), meta={"pages": len(reader.pages)})
    empty_pages = 0
    for pno, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # pragma: no cover
            text = ""
            doc.meta.setdefault("errors", []).append(f"p.{pno}: {e}")
        lines = [ln.rstrip() for ln in N.clean_text(text).split("\n") if ln.strip()]
        if not lines:
            empty_pages += 1
        for ln in lines:
            doc.blocks.append(Block(kind="para", text=ln, page=pno))
    if empty_pages:
        doc.meta["note"] = f"텍스트 레이어가 없는 페이지 {empty_pages}개 (스캔 이미지 PDF 는 OCR 후 비교 필요)"
    return doc


# ------------------------------------------------------------------------------ CSV / TSV
def extract_csv(path: str | Path, sheet_name: str = "CSV"):
    from .excel import WorkbookSnap

    path = str(path)
    text, enc = _read_text_file(path)
    sample = text[:20000]
    delim = "\t" if path.lower().endswith((".tsv", ".tab")) else ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        pass
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    g = Grid(grid_id=f"sheet:{sheet_name}", name=sheet_name, is_excel=True)
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            t = N.clean_text(v)
            cv = N.canon(t)
            if cv is not None:
                g.cells[(r, c)] = GridCell(canon=cv, display=t)
    snap = WorkbookSnap(path=path, sha256=file_sha256(path), sheets=[g])
    snap.props = {sheet_name: {}}
    snap.meta = {"encoding": enc, "delimiter": delim}
    return snap
