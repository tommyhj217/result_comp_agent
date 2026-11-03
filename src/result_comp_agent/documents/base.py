"""문서 핸들러 기본 클래스와 공용 유틸."""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..diff import normalize as N
from ..diff.builder import ChangeBuilder
from ..models import IntegrityReport


class DocumentError(RuntimeError):
    """비교 실패 사유를 종류(kind)와 함께 전달.
    kind: file_not_found | unsupported | missing_dependency | parse_error | format_mismatch | timeout | crash | error"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


DOC_ERROR_LABELS = {
    "file_not_found": "파일 없음",
    "unsupported": "지원하지 않는 형식",
    "missing_dependency": "필요 라이브러리 없음",
    "parse_error": "문서 읽기 실패(손상/암호/형식 오류)",
    "format_mismatch": "원본·수정본 형식 불일치",
    "timeout": "비교 시간 초과",
    "crash": "비교 프로세스 비정상 종료",
    "error": "비교 오류",
}


@dataclass
class DocUnit:
    """문서의 조회 단위 (GPT 도구 read_document/search_documents 와 rca inspect 에서 사용)."""

    locator: str
    text: str
    kind: str = "block"


@dataclass
class CompareOptions:
    compare_styles: bool = True
    compare_geometry: bool = True
    header_rows: dict[str, int] = field(default_factory=dict)
    key_columns: dict[str, str] = field(default_factory=dict)


@dataclass
class CompareOutput:
    alignment: dict[str, Any]
    integrity: IntegrityReport
    base_sha256: str
    rev_sha256: str
    notes: list[str] = field(default_factory=list)


class DocumentHandler:
    name: str = ""
    artifact: str = ""
    extensions: tuple[str, ...] = ()
    description: str = ""
    requires: tuple[str, ...] = ()  # import 가능해야 하는 모듈
    install_hint: str = ""
    reliability_note: str = ""  # 보고서에 표시할 이 형식 비교의 한계

    def available(self) -> tuple[bool, str]:
        for mod in self.requires:
            try:
                importlib.import_module(mod)
            except Exception:
                return False, f"'{mod}' 모듈 필요" + (f" — {self.install_hint}" if self.install_hint else "")
        return True, "사용 가능"

    def compare(self, baseline: str, revised: str, builder: ChangeBuilder, opts: CompareOptions) -> CompareOutput:  # pragma: no cover
        raise NotImplementedError

    def outline(self, path: str) -> list[DocUnit]:  # pragma: no cover
        raise NotImplementedError


class FlowDocHandler(DocumentHandler):
    """흐름형 문서(문단/제목/표의 순서) 핸들러 — extract() 만 구현하면 된다."""

    def extract(self, path: str):  # -> FlowDoc
        raise NotImplementedError  # pragma: no cover

    def compare(self, baseline: str, revised: str, builder: ChangeBuilder, opts: CompareOptions) -> CompareOutput:
        from ..diff.flow_diff import diff_flow

        b, r = self.extract(baseline), self.extract(revised)
        alignment, integrity = diff_flow(b, r, builder, self.artifact)
        notes = []
        for label, doc in (("원본", b), ("수정본", r)):
            if doc.meta.get("note"):
                notes.append(f"{label}: {doc.meta['note']}")
        if self.reliability_note:
            notes.append(self.reliability_note)
        return CompareOutput(alignment, integrity, b.sha256, r.sha256, notes)

    def outline(self, path: str) -> list[DocUnit]:
        doc = self.extract(path)
        secs = doc.sections()
        out = []
        for i, (bl, sec) in enumerate(zip(doc.blocks, secs), start=1):
            page = f" p.{bl.page}" if bl.page else ""
            loc = f"¶{i}{page}" + (f" [{sec}]" if sec else "")
            text = bl.table_text() if bl.is_table else bl.text
            out.append(DocUnit(loc, text, bl.kind))
        for c in doc.comments:
            out.append(DocUnit(f"메모 ¶{(c.block_index or 0) + 1}", f"{c.author}: {c.text}", "comment"))
        return out


_RANGE_RE = re.compile(r"^(.*?)(\d+)\s*[:~\-]\s*(\d+)$")
_TRAIL_NUM = re.compile(r"(\d+)(?!.*\d)")


def select_units(units: list[DocUnit], locator: str, limit: int = 40) -> list[DocUnit]:
    """locator 로 단위 선택: 비우면 처음부터 / 'Sheet!3:10' 처럼 범위 / 부분 일치(위치 → 본문 순)."""
    loc = (locator or "").strip()
    if not loc:
        return units[:limit]
    m = _RANGE_RE.match(loc)
    if m:
        prefix, a, b = m.group(1).strip().casefold(), int(m.group(2)), int(m.group(3))
        lo, hi = min(a, b), max(a, b)
        sel = []
        for u in units:
            ul = u.locator.casefold()
            if prefix and not ul.startswith(prefix):
                continue
            nums = [int(x) for x in re.findall(r"\d+", u.locator[len(prefix):] if prefix else u.locator)]
            if nums and lo <= nums[0] <= hi:
                sel.append(u)
        if sel:
            return sel[:limit]
    lc = loc.casefold()
    exact = [u for u in units if u.locator.casefold() == lc]
    if exact:
        return exact[:limit]
    pref = [u for u in units if u.locator.casefold().startswith(lc)]
    if pref:
        return pref[:limit]
    sub = [u for u in units if lc in u.locator.casefold()]
    if sub:
        return sub[:limit]
    return [u for u in units if lc in u.text.casefold()][:limit]


def search_units(units: list[DocUnit], query: str, limit: int = 20) -> list[dict]:
    q = N.collapse_ws(query or "").casefold()
    if not q:
        return []
    out = []
    for u in units:
        t = N.collapse_ws(u.text)
        k = t.casefold().find(q)
        if k >= 0:
            out.append({"locator": u.locator, "kind": u.kind, "snippet": t[max(0, k - 60) : k + len(q) + 80]})
            if len(out) >= limit:
                break
    return out
