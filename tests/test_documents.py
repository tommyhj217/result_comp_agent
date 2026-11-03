"""문서 범위 확장: Word·Markdown·텍스트·PDF·CSV, Excel 시트 속성, 핸들러 레지스트리/플러그인, 격리 실행."""
from __future__ import annotations

import docx
import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from pdfgen import make_pdf
from result_comp_agent.analysis.aggregator import aggregate
from result_comp_agent.analysis.classifier import classify_all
from result_comp_agent.analysis.recommender import recommend
from result_comp_agent.diff.compare import compare_files, compare_files_isolated
from result_comp_agent.documents import DocumentError, handler_for, list_handlers, search_units, select_units
from result_comp_agent.models import ChangeType


def _cmp(a, b, pid="p-1"):
    return compare_files(a, b, pair_id=pid, agent_id="a")


def _docx_base(path):
    d = docx.Document()
    d.add_heading("제품 기획서", 0)
    d.add_heading("1. 개요", 1)
    d.add_paragraph("AI 일정관리 앱을 개발한다.")
    d.add_paragraph("목표 사용자는 직장인이다.")
    d.add_heading("2. 요구사항", 1)
    t = d.add_table(rows=3, cols=3)
    for r, row in enumerate([["ID", "요구사항", "우선순위"], ["R1", "로그인", "상"], ["R2", "알림", "중"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    d.add_heading("3. 일정", 1)
    d.add_paragraph("MVP 6개월")
    d.save(path)
    return path


def test_word_diff_sections_tables_comments(tmp_path):
    a = _docx_base(tmp_path / "a.docx")
    d = docx.Document(a)
    d.paragraphs[3].runs[0].text = "목표 사용자는 20~30대 직장인과 프리랜서이다."
    d.tables[0].cell(2, 2).text = "상"
    d.add_heading("4. 리스크", 1)
    p = d.add_paragraph("개인정보 규제 대응 필요")
    d.add_comment(p.runs, text="법무팀 확인", author="kim")
    d.save(tmp_path / "b.docx")
    r = _cmp(a, tmp_path / "b.docx")
    assert r.integrity.status == "PASS", r.integrity.failures
    by = {}
    for c in r.changes:
        by.setdefault(c.change_type, []).append(c)
    tm = by[ChangeType.TEXT_MODIFIED][0]
    assert tm.location.section == "1. 개요" and tm.detail["numbers_after"] == ["20", "30"]
    cell = by[ChangeType.CELL_MODIFIED][0]
    assert (cell.before, cell.after, cell.location.column_label, cell.location.row_label) == ("중", "상", "우선순위", "R2")
    heading = next(c for c in by[ChangeType.TEXT_ADDED] if c.location.part == "heading")
    roles = {(x.text, x.role) for x in heading.anchors}
    assert ("4. 리스크", "new_section") in roles and ("3. 일정", "neighbor_left") in roles
    assert by[ChangeType.COMMENT_ADDED][0].after == "법무팀 확인"
    classify_all(r.changes)
    assert heading.category == "STRUCTURE_CHANGE"


def test_word_tracked_changes_accept_all_view(tmp_path):
    a = _docx_base(tmp_path / "a.docx")
    d = docx.Document(a)
    p = d.paragraphs[2]._p
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "1")
    ins.set(qn("w:author"), "kim")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = " (모바일 우선)"
    r.append(t)
    ins.append(r)
    p.append(ins)
    dele = OxmlElement("w:del")
    dele.set(qn("w:id"), "2")
    dele.set(qn("w:author"), "kim")
    r2 = OxmlElement("w:r")
    dt = OxmlElement("w:delText")
    dt.text = "삭제된문구"
    r2.append(dt)
    dele.append(r2)
    p.append(dele)
    d.save(tmp_path / "b.docx")
    res = _cmp(a, tmp_path / "b.docx")
    assert res.integrity.status == "PASS"
    assert [c.after for c in res.changes] == ["AI 일정관리 앱을 개발한다. (모바일 우선)"]
    assert any("변경 내용 추적" in n for n in res.notes)


def test_markdown_and_text(tmp_path):
    (tmp_path / "a.md").write_text("# 기획\n\n## 개요\n앱을 만든다.\n\n- 항목1\n- 항목2\n\n| ID | 이름 |\n|---|---|\n| 1 | 가 |\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# 기획\n\n## 개요\n앱을 빠르게 만든다.\n\n- 항목1\n- 항목2\n- 항목3\n\n| ID | 이름 |\n|---|---|\n| 1 | 가 |\n| 2 | 나 |\n\n## 리스크\n일정 지연\n", encoding="utf-8")
    r = _cmp(tmp_path / "a.md", tmp_path / "b.md")
    assert r.integrity.status == "PASS"
    types = sorted(c.change_type.value for c in r.changes)
    assert types == ["row_added", "text_added", "text_added", "text_added", "text_modified"]
    (tmp_path / "a.txt").write_bytes("회의록\n결정: A안\n담당: 김\n".encode("cp949"))
    (tmp_path / "b.txt").write_text("회의록\n결정: B안\n담당: 김\n후속: 법무 검토\n", encoding="utf-8")
    r2 = _cmp(tmp_path / "a.txt", tmp_path / "b.txt")
    assert r2.integrity.status == "PASS" and len(r2.changes) == 2


def test_pdf_text_diff(tmp_path):
    make_pdf(tmp_path / "a.pdf", [["Market size 1200", "Growth 8 percent"], ["Target office workers"]])
    make_pdf(tmp_path / "b.pdf", [["Market size 1850", "Growth 8 percent"], ["Target office workers", "Source survey 2025"]])
    r = _cmp(tmp_path / "a.pdf", tmp_path / "b.pdf")
    assert r.integrity.status == "PASS", r.integrity.failures
    mod = next(c for c in r.changes if c.change_type == ChangeType.TEXT_MODIFIED)
    assert mod.location.page == 1 and mod.after == "Market size 1850"
    add = next(c for c in r.changes if c.change_type == ChangeType.TEXT_ADDED)
    assert add.location.page == 2 and "Source survey" in add.after
    assert any("PDF" in n for n in r.notes)


def test_csv_encodings_and_format_only(tmp_path):
    (tmp_path / "a.csv").write_bytes("ID,이름,금액\n1,가,1000\n2,나,2000\n".encode("cp949"))
    (tmp_path / "b.csv").write_text('ID,이름,금액,비고\n1,가,"1,000",\n2,나,2500,인상\n3,다,300,신규\n', encoding="utf-8")
    r = _cmp(tmp_path / "a.csv", tmp_path / "b.csv")
    assert r.integrity.status == "PASS"
    fmt = [c for c in r.changes if c.format_only]
    assert len(fmt) == 1 and fmt[0].after == "1,000"
    assert {c.change_type for c in r.changes} >= {ChangeType.COLUMN_ADDED, ChangeType.ROW_ADDED, ChangeType.CELL_MODIFIED}
    assert any("인코딩" in n for n in r.notes)


def test_excel_sheet_properties(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "요구사항"
    ws.append(["ID", "요구사항", "우선순위"])
    for i in range(1, 5):
        ws.append([f"R{i}", f"기능{i}", "중"])
    wb.save(tmp_path / "a.xlsx")
    wb = load_workbook(tmp_path / "a.xlsx")
    ws = wb.active
    dv = DataValidation(type="list", formula1='"상,중,하"', allow_blank=True)
    dv.add("C2:C5")
    ws.add_data_validation(dv)
    ws.conditional_formatting.add("C2:C5", CellIsRule(operator="equal", formula=['"상"'], fill=PatternFill("solid", fgColor="FFFF00")))
    ws.freeze_panes = "A2"
    ws.insert_rows(3)  # 행 삽입이 있어도 속성 비교는 열 헤더 기준이라 흔들리지 않아야 함
    ws.delete_rows(3)
    wb.save(tmp_path / "b.xlsx")
    r = _cmp(tmp_path / "a.xlsx", tmp_path / "b.xlsx", "x-1")
    assert r.integrity.status == "PASS"
    props = {c.detail["property"]: c for c in r.changes if c.change_type == ChangeType.SHEET_PROPERTY_CHANGED}
    assert set(props) == {"data_validation", "conditional_format", "freeze_panes"}
    dvc = props["data_validation"]
    assert not dvc.format_only and "우선순위" in dvc.after and '"상,중,하"' in dvc.after
    assert props["conditional_format"].format_only
    classify_all(r.changes)
    pats = aggregate("a", [r])
    recs = recommend("a", pats, {c.id: c for c in r.changes}, 1)
    assert any("입력 규칙(데이터 유효성) 코드화" in x.title for x in recs)


def test_registry_and_errors(tmp_path):
    names = {h.name for h in list_handlers()}
    assert {"excel", "powerpoint", "word", "csv", "markdown", "text", "pdf"} <= names
    with pytest.raises(DocumentError) as e:
        handler_for("x.hwp")
    assert e.value.kind == "unsupported"
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DocumentError) as e2:
        _cmp(tmp_path / "a.md", tmp_path / "missing.md")
    assert e2.value.kind == "file_not_found"
    with pytest.raises(DocumentError) as e3:
        _cmp(tmp_path / "a.md", tmp_path / "a.txt")
    assert e3.value.kind == "format_mismatch"
    (tmp_path / "bad.xlsx").write_bytes(b"not a zip")
    (tmp_path / "bad2.xlsx").write_bytes(b"not a zip")
    with pytest.raises(DocumentError) as e4:
        _cmp(tmp_path / "bad.xlsx", tmp_path / "bad2.xlsx")
    assert e4.value.kind == "parse_error"


def test_outline_select_search(tmp_path):
    _docx_base(tmp_path / "a.docx")
    units = handler_for("a.docx").outline(str(tmp_path / "a.docx"))
    assert any(u.kind == "table" for u in units)
    assert select_units(units, "¶3")[0].text == "AI 일정관리 앱을 개발한다."
    assert [u.locator for u in select_units(units, "¶2:3")] == ["¶2 [1. 개요]", "¶3 [1. 개요]"]
    assert select_units(units, "2. 요구사항")[0].text == "2. 요구사항"
    assert search_units(units, "MVP")[0]["locator"].startswith("¶")


def test_plugin_handler_and_tool(tmp_path):
    from result_comp_agent import documents
    from result_comp_agent.plugins import load_plugins
    from result_comp_agent.tools import DEFAULT_REGISTRY, default_registry

    plug = tmp_path / "foo_plugin.py"
    plug.write_text('''
from result_comp_agent.documents import FlowDocHandler, register_handler
from result_comp_agent.diff.flow_diff import Block, FlowDoc
from result_comp_agent.extractors.excel import file_sha256
from result_comp_agent.tools import register_tool

class FooHandler(FlowDocHandler):
    name, artifact, extensions, description = "foo", "foo", (".foo",), "줄 단위 사내 포맷"
    def extract(self, path):
        lines = open(path, encoding="utf-8").read().splitlines()
        return FlowDoc(path=str(path), sha256=file_sha256(path), blocks=[Block(kind="heading" if l.startswith("=") else "para", text=l.strip("= ")) for l in lines if l.strip()])

register_handler(FooHandler())

@register_tool("count_words_test", "문장 단어 수", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def count_words(ctx, text):
    return {"words": len(text.split())}
''', encoding="utf-8")
    try:
        loaded, errors = load_plugins([str(plug)], include_env=False, include_entry_points=False)
        assert loaded and not errors
        (tmp_path / "a.foo").write_text("= 개요\n첫 문장\n", encoding="utf-8")
        (tmp_path / "b.foo").write_text("= 개요\n첫 문장 수정\n= 리스크\n", encoding="utf-8")
        r = _cmp(tmp_path / "a.foo", tmp_path / "b.foo")
        assert r.artifact == "foo" and r.integrity.status == "PASS" and len(r.changes) == 2
        out, err = default_registry().call("count_words_test", '{"text": "a b c"}', None)
        assert err is None and '"words": 3' in out
        bad = load_plugins(["no_such_module_xyz"], include_env=False, include_entry_points=False)[1]
        assert bad and "로드 실패" in bad[0]
    finally:
        documents._REGISTRY.pop("foo", None)
        DEFAULT_REGISTRY._tools.pop("count_words_test", None)


def test_isolated_compare_success_timeout_crash(tmp_path):
    (tmp_path / "a.md").write_text("# A\n문단\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# A\n문단 수정\n", encoding="utf-8")
    kw = dict(baseline=str(tmp_path / "a.md"), revised=str(tmp_path / "b.md"), pair_id="p", agent_id="a")
    ok = compare_files_isolated(30, **kw)
    assert ok.integrity.status == "PASS" and len(ok.changes) == 1 and ok.seconds is not None
    with pytest.raises(DocumentError) as e:
        compare_files_isolated(0.5, target="slow_target:compare_slow", **kw)
    assert e.value.kind == "timeout" and "0.5초" in str(e.value)
    with pytest.raises(DocumentError) as e2:
        compare_files_isolated(10, target="slow_target:compare_crash", **kw)
    assert e2.value.kind == "crash"
    with pytest.raises(DocumentError) as e3:
        compare_files_isolated(10, baseline=str(tmp_path / "zz.md"), revised=str(tmp_path / "b.md"), pair_id="p", agent_id="a")
    assert e3.value.kind == "file_not_found"


def test_example_plugin_json_handler_and_tool(tmp_path):
    from pathlib import Path

    from result_comp_agent import documents
    from result_comp_agent.plugins import load_plugins
    from result_comp_agent.tools import DEFAULT_REGISTRY, default_registry
    from result_comp_agent.tools.context import ToolContext

    plug = Path(__file__).resolve().parent.parent / "examples" / "plugins" / "sample_plugin.py"
    try:
        loaded, errors = load_plugins([str(plug)], include_env=False, include_entry_points=False)
        assert not errors
        (tmp_path / "a.json").write_text('{"api": {"name": "login", "timeout": 2}, "owner": "기획팀"}', encoding="utf-8")
        (tmp_path / "b.json").write_text('{"api": {"name": "login", "timeout": 1, "retry": 3}, "owner": "플랫폼팀"}', encoding="utf-8")
        r = _cmp(tmp_path / "a.json", tmp_path / "b.json")
        assert r.integrity.status == "PASS"
        assert sorted((c.change_type.value, c.after) for c in r.changes) == [
            ("text_added", "api.retry: 3"), ("text_modified", "api.timeout: 1"), ("text_modified", 'owner: "플랫폼팀"')]
        classify_all(r.changes)
        out, err = default_registry().call("summarize_changes_by_location", "{}", ToolContext(agent_id="a", changes={c.id: c for c in r.changes}))
        assert err is None and '"api"' in out
    finally:
        documents._REGISTRY.pop("json", None)
        DEFAULT_REGISTRY._tools.pop("summarize_changes_by_location", None)
