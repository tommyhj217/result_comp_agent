from __future__ import annotations

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

from result_comp_agent.diff.builder import ChangeBuilder
from result_comp_agent.diff.compare import compare_files
from result_comp_agent.diff.excel_diff import diff_workbooks
from result_comp_agent.extractors.excel import extract_workbook
from result_comp_agent.models import ArtifactType, ChangeType

HEAD = ["요구사항ID", "구분", "요구사항명", "상세설명", "우선순위", "담당자"]
ROWS = [
    ["REQ-001", "기능", "회원가입", "이메일 가입", "상", "백엔드팀"],
    ["REQ-002", "기능", "로그인", "소셜 로그인", "상", "백엔드팀"],
    ["REQ-003", "기능", "검색", "상품 검색", "중", "검색팀"],
    ["REQ-004", "비기능", "성능", "응답 2초 이내", "중", "인프라팀"],
    ["REQ-005", "기능", "알림", "푸시 알림", "하", "모바일팀"],
]


def _wb(path, rows=ROWS, head=HEAD, title="요구사항"):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(["요구사항 정의서"])
    ws.append(head)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _diff(a, b):
    builder = ChangeBuilder("p", "a", ArtifactType.XLSX)
    al, integ = diff_workbooks(extract_workbook(a), extract_workbook(b), builder)
    return builder.changes, al, integ


def test_identical_files_have_no_changes(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    b = _wb(tmp_path / "b.xlsx")
    ch, _, integ = _diff(a, b)
    assert ch == []
    assert integ.status == "PASS"


def test_key_alignment_column_insert_row_ops(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    wb = load_workbook(a)
    ws = wb.active
    ws.insert_cols(6)
    ws.cell(2, 6).value = "리스크"
    for r, v in zip(range(3, 8), ["낮음", "중간", "높음", "중간", "낮음"]):
        ws.cell(r, 6).value = v
    ws["E5"] = "상"  # REQ-003 우선순위 중→상
    ws.delete_rows(7)  # REQ-005 삭제
    ws.append(["REQ-006", "비기능", "보안", "개인정보 암호화", "상", "높음", "보안팀"])
    ws["C3"].comment = Comment("법무 검토", "kim")
    b = tmp_path / "b.xlsx"
    wb.save(b)
    ch, al, integ = _diff(a, b)
    assert integ.status == "PASS", integ.failures
    assert al["sheets"][0]["grid"]["key_column"] == "요구사항ID"
    types = [c.change_type for c in ch]
    assert types.count(ChangeType.COLUMN_ADDED) == 1
    assert types.count(ChangeType.ROW_ADDED) == 1
    assert types.count(ChangeType.ROW_REMOVED) == 1
    assert types.count(ChangeType.COMMENT_ADDED) == 1
    mods = [c for c in ch if c.change_type == ChangeType.CELL_MODIFIED]
    # 열 삽입으로 담당자 열이 밀렸어도 셀 수정은 정확히 1건(우선순위)만 보고되어야 한다
    assert len(mods) == 1
    m = mods[0]
    assert (m.before, m.after) == ("중", "상")
    assert m.location.row_label == "REQ-003" and m.location.column_label == "우선순위"
    assert m.pairing == "key"
    col = next(c for c in ch if c.change_type == ChangeType.COLUMN_ADDED)
    roles = {(x.text, x.role) for x in col.anchors}
    assert ("우선순위", "neighbor_left") in roles and ("담당자", "neighbor_right") in roles


def test_dp_alignment_without_key(tmp_path):
    head = ["항목", "설명", "비고"]
    rows = [["가", "첫째 설명", "x"], ["나", "둘째 설명", "y"], ["다", "셋째 설명", "z"], ["라", "넷째 설명", "w"]]
    a = _wb(tmp_path / "a.xlsx", rows, head, "목록")
    new_rows = rows[:2] + [["나2", "새로 추가한 설명", "n"]] + rows[2:]
    new_rows[3] = ["다", "셋째 설명 (보완)", "z"]
    b = _wb(tmp_path / "b.xlsx", new_rows, head, "목록")
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS", integ.failures
    assert [c.change_type for c in ch].count(ChangeType.ROW_ADDED) == 1
    mods = [c for c in ch if c.change_type == ChangeType.CELL_MODIFIED]
    assert len(mods) == 1 and mods[0].after == "셋째 설명 (보완)"
    assert mods[0].pairing == "similarity"


def test_format_only_detection(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["이름", "금액", "메모"])
    ws.append(["A", 1000, "확인 완료"])
    ws.append(["B", 2000, "검토"])
    a = tmp_path / "a.xlsx"
    wb.save(a)
    ws["B2"] = "1,000"
    ws["C3"] = "검토 "
    ws["C2"] = "확인  완료"
    b = tmp_path / "b.xlsx"
    wb.save(b)
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS"
    assert len(ch) == 3 and all(c.format_only for c in ch)


def test_formula_ref_shift_flag(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["항목", "값"])
    ws.append(["a", 1])
    ws.append(["b", 2])
    ws.append(["합계", "=SUM(B2:B3)"])
    a = tmp_path / "a.xlsx"
    wb.save(a)
    ws["B4"] = "=SUM(B2:B4)"
    b = tmp_path / "b.xlsx"
    wb.save(b)
    ch, _, _ = _diff(a, b)
    assert len(ch) == 1 and ch[0].detail.get("formula") and ch[0].detail.get("formula_ref_shift")


def test_sheet_added_removed_renamed(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    wb = load_workbook(a)
    wb.create_sheet("일정").append(["단계", "시작"])
    wb["요구사항"].title = "요구사항목록"
    b = tmp_path / "b.xlsx"
    wb.save(b)
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS"
    types = {c.change_type for c in ch}
    assert ChangeType.SHEET_RENAMED in types and ChangeType.SHEET_ADDED in types


def test_integrity_detects_tampered_change_list(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    wb = load_workbook(a)
    wb.active["D4"] = "소셜 로그인 + 2FA"
    wb.active.append(["REQ-006", "기능", "탈퇴", "회원 탈퇴", "하", "백엔드팀"])
    b = tmp_path / "b.xlsx"
    wb.save(b)
    from result_comp_agent.diff.excel_diff import diff_workbooks as dw

    ea, eb = extract_workbook(a), extract_workbook(b)
    # 1) 정상
    builder = ChangeBuilder("p", "a", ArtifactType.XLSX)
    _, ok = dw(ea, eb, builder)
    assert ok.status == "PASS"
    # 2) 변경 하나를 누락시키면(diff 엔진 결함 가정) 재구성 검증이 실패해야 한다
    builder2 = ChangeBuilder("p", "a", ArtifactType.XLSX)
    orig_add = builder2.add
    dropped = []

    def drop_row_added(ct, *args, **kw):
        if ct == ChangeType.ROW_ADDED and not dropped:
            dropped.append(1)
            return None
        return orig_add(ct, *args, **kw)

    builder2.add = drop_row_added
    _, bad = dw(ea, eb, builder2)
    assert bad.status == "FAIL"
    assert any("재구성 불일치" in f or "추가 보고도 없음" in f for f in bad.failures)


def test_integrity_detects_wrong_before_value(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    wb = load_workbook(a)
    wb.active["D4"] = "소셜 로그인 + 2FA"
    b = tmp_path / "b.xlsx"
    wb.save(b)
    ea, eb = extract_workbook(a), extract_workbook(b)
    builder = ChangeBuilder("p", "a", ArtifactType.XLSX)
    al, _ = diff_workbooks(ea, eb, builder)
    from result_comp_agent.diff.grid import GridAlignment, verify_grid

    g = al["sheets"][0]["grid"]
    gal = GridAlignment(grid_id=g["grid_id"], base_grid_id=g["base_grid_id"], row_pairs=[tuple(x) for x in g["row_pairs"]], col_pairs=[tuple(x) for x in g["col_pairs"]])
    mod = next(c for c in builder.changes if c.change_type == ChangeType.CELL_MODIFIED)
    mod.detail["before_canon"] = "s:조작된 값"
    _, failures = verify_grid(ea.sheets[0], eb.sheets[0], gal, builder.changes)
    assert any("이전 값이 원본과 다름" in f for f in failures)


def test_compare_files_facade(tmp_path):
    a = _wb(tmp_path / "a.xlsx")
    wb = load_workbook(a)
    wb.active["D3"] = "이메일 + 휴대폰 본인인증 가입"
    b = tmp_path / "b.xlsx"
    wb.save(b)
    res = compare_files(a, b, pair_id="req-1", agent_id="req")
    assert res.integrity.status == "PASS"
    assert res.stats["content"] == 1
    assert res.changes[0].id == "req-1:C0001"
