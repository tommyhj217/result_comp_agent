"""PowerPoint(.pptx) 스냅샷 추출.

슬라이드별: slide_id, 레이아웃, 제목, 도형(텍스트 문단/표/차트/이미지/그룹 내부), 발표자 노트.
빈 문단(공백만 있는 문단)은 내용이 없으므로 제외한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.picture import Picture

from ..diff import normalize as N
from ..diff.grid import Grid, GridCell
from .excel import file_sha256

Para = tuple[str, int]  # (text, level)


@dataclass
class ShapeSnap:
    key: str
    shape_id: int
    name: str
    kind: str  # text | table | picture | chart | other
    placeholder: Optional[str] = None
    left: Optional[int] = None
    top: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    paragraphs: list[Para] = field(default_factory=list)
    table: Optional[Grid] = None
    image_sha1: Optional[str] = None
    chart_sig: Optional[dict[str, Any]] = None

    @property
    def text(self) -> str:
        if self.kind == "table" and self.table is not None:
            return "\n".join(c.display for _, c in sorted(self.table.cells.items()) if c.canon is not None)
        return "\n".join(p for p, _ in self.paragraphs)

    def content(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.paragraphs:
            d["paragraphs"] = [p for p, _ in self.paragraphs]
        if self.table is not None:
            d["table"] = [[r, c, v] for (r, c), v in sorted(self.table.all_canon().items())]
        if self.image_sha1:
            d["image_sha1"] = self.image_sha1
        if self.chart_sig:
            d["chart"] = self.chart_sig
        return d


@dataclass
class SlideSnap:
    index: int
    slide_id: int
    layout: str
    title: str
    shapes: list[ShapeSnap] = field(default_factory=list)
    notes: list[Para] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        parts = [s.text for s in self.shapes if s.text]
        return "\n".join(parts)

    def content(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "layout": self.layout,
            "shapes": [s.content() for s in self.shapes],
            "notes": [p for p, _ in self.notes],
        }


@dataclass
class PresentationSnap:
    path: str
    sha256: str
    slide_width: int
    slide_height: int
    slides: list[SlideSnap] = field(default_factory=list)


def _paragraphs(text_frame) -> list[Para]:
    out: list[Para] = []
    for p in text_frame.paragraphs:
        t = N.clean_text(p.text)
        if t.strip():
            out.append((t, int(p.level or 0)))
    return out


def _iter_shapes(shapes, prefix: str = ""):
    for shp in shapes:
        try:
            st = shp.shape_type
        except Exception:  # pragma: no cover - 일부 graphicFrame
            st = None
        if st == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(shp.shapes, prefix=f"{prefix}{shp.shape_id}/")
        else:
            yield shp, prefix


def _chart_sig(chart) -> dict[str, Any]:
    try:
        sig: dict[str, Any] = {"chart_type": str(chart.chart_type)}
        plots = list(chart.plots)
        sig["categories"] = [str(c) for c in plots[0].categories] if plots else []
        sig["series"] = [
            {"name": s.name, "values": [None if v is None else N.num_str(v) for v in s.values]}
            for p in plots
            for s in p.series
        ]
        sig["title"] = chart.chart_title.text_frame.text if chart.has_title else None
        return sig
    except Exception as e:  # pragma: no cover
        return {"error": str(e)}


def extract_presentation(path: str | Path) -> PresentationSnap:
    path = str(path)
    prs = Presentation(path)
    snap = PresentationSnap(path=path, sha256=file_sha256(path), slide_width=int(prs.slide_width or 0), slide_height=int(prs.slide_height or 0))
    for idx, slide in enumerate(prs.slides, start=1):
        shapes: list[ShapeSnap] = []
        title_text = ""
        try:
            tshape = slide.shapes.title
            if tshape is not None and tshape.has_text_frame:
                title_text = N.collapse_ws(tshape.text_frame.text)
        except Exception:  # pragma: no cover
            pass
        for shp, prefix in _iter_shapes(slide.shapes):
            ph = None
            if getattr(shp, "is_placeholder", False):
                try:
                    ph = str(shp.placeholder_format.type).split(".")[-1].split(" ")[0]
                except Exception:  # pragma: no cover
                    ph = "placeholder"
            s = ShapeSnap(
                key=f"{prefix}{shp.shape_id}",
                shape_id=int(shp.shape_id),
                name=shp.name,
                kind="other",
                placeholder=ph,
                left=shp.left,
                top=shp.top,
                width=shp.width,
                height=shp.height,
            )
            if getattr(shp, "has_table", False) and shp.has_table:
                s.kind = "table"
                g = Grid(grid_id="", name=shp.name, is_excel=False)
                for r, row in enumerate(shp.table.rows, start=1):
                    for c, cell in enumerate(row.cells, start=1):
                        if getattr(cell, "is_spanned", False):
                            continue
                        t = N.clean_text(cell.text)
                        cv = N.canon(t)
                        if cv is not None:
                            g.cells[(r, c)] = GridCell(canon=cv, display=t)
                s.table = g
            elif getattr(shp, "has_chart", False) and shp.has_chart:
                s.kind = "chart"
                s.chart_sig = _chart_sig(shp.chart)
            elif isinstance(shp, Picture):
                s.kind = "picture"
                try:
                    s.image_sha1 = shp.image.sha1
                except Exception:  # pragma: no cover - 링크된 이미지 등
                    s.image_sha1 = None
            elif getattr(shp, "has_text_frame", False) and shp.has_text_frame:
                s.kind = "text"
                s.paragraphs = _paragraphs(shp.text_frame)
            shapes.append(s)
        if not title_text:
            first = next((s for s in shapes if s.kind == "text" and s.paragraphs), None)
            title_text = N.collapse_ws(first.paragraphs[0][0]) if first else ""
        notes: list[Para] = []
        if slide.has_notes_slide:
            ntf = slide.notes_slide.notes_text_frame
            if ntf is not None:
                notes = _paragraphs(ntf)
        sid = int(slide.slide_id)
        for s in shapes:
            if s.table is not None:
                s.table.grid_id = f"slide[{sid}]/shape[{s.key}]/table"
        snap.slides.append(
            SlideSnap(index=idx, slide_id=sid, layout=slide.slide_layout.name, title=title_text, shapes=shapes, notes=notes)
        )
    return snap
