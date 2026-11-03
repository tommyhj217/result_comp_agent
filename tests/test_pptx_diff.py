from __future__ import annotations

from pptx import Presentation
from pptx.util import Inches

from result_comp_agent.diff.builder import ChangeBuilder
from result_comp_agent.diff.pptx_diff import diff_presentations
from result_comp_agent.extractors.pptx import extract_presentation
from result_comp_agent.models import ArtifactType, ChangeType


def _deck(path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "시장분석 보고서"
    s.placeholders[1].text = "플랜잇"

    def bullets(title, items):
        sl = prs.slides.add_slide(prs.slide_layouts[1])
        sl.shapes.title.text = title
        tf = sl.placeholders[1].text_frame
        tf.text = items[0]
        for b in items[1:]:
            tf.add_paragraph().text = b

    bullets("시장 개요", ["시장 규모 1,200억원", "연평균 성장률 8%", "개인화 트렌드"])
    sl = prs.slides.add_slide(prs.slide_layouts[5])
    sl.shapes.title.text = "경쟁사 분석"
    t = sl.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(1.5)).table
    for r, row in enumerate([["경쟁사", "강점", "약점"], ["A", "인지도", "AI 부재"], ["B", "협업", "복잡한 UI"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    bullets("타겟 고객", ["20~30대 직장인", "프리랜서"])
    bullets("결론", ["AI 추천으로 차별화", "MVP 6개월"])
    prs.save(path)
    return path


def _diff(a, b):
    builder = ChangeBuilder("m", "ma", ArtifactType.PPTX)
    al, integ = diff_presentations(extract_presentation(a), extract_presentation(b), builder)
    return builder.changes, al, integ


def _move_last_to(prs, pos):
    ids = prs.slides._sldIdLst
    items = list(ids)
    ids.remove(items[-1])
    ids.insert(pos, items[-1])


def test_identical(tmp_path):
    a = _deck(tmp_path / "a.pptx")
    b = _deck(tmp_path / "b.pptx")
    ch, _, integ = _diff(a, b)
    assert ch == [] and integ.status == "PASS"


def test_user_edits_detected_and_verified(tmp_path):
    a = _deck(tmp_path / "a.pptx")
    prs = Presentation(a)
    tf = prs.slides[1].placeholders[1].text_frame
    tf.paragraphs[0].runs[0].text = "시장 규모 1,850억원 (통계청 2025)"
    tf.add_paragraph().text = "출처: 통계청"
    table = [s for s in prs.slides[2].shapes if s.has_table][0].table
    table.cell(2, 2).text = "복잡한 UI, 평점 3.2"
    prs.slides[3].notes_slide.notes_text_frame.text = "인터뷰 20건 근거"
    new = prs.slides.add_slide(prs.slide_layouts[1])
    new.shapes.title.text = "가격 전략"
    new.placeholders[1].text_frame.text = "월 4,900원"
    _move_last_to(prs, 4)
    b = tmp_path / "b.pptx"
    prs.save(b)
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS", integ.failures
    by = {}
    for c in ch:
        by.setdefault(c.change_type, []).append(c)
    assert len(by[ChangeType.TEXT_MODIFIED]) == 1
    tm = by[ChangeType.TEXT_MODIFIED][0]
    assert tm.detail["numbers_before"] != tm.detail["numbers_after"]
    assert len(by[ChangeType.TEXT_ADDED]) == 2  # 본문 1 + 노트 1
    assert any(c.location.part == "notes" for c in by[ChangeType.TEXT_ADDED])
    assert len(by[ChangeType.CELL_MODIFIED]) == 1 and by[ChangeType.CELL_MODIFIED][0].location.column_label == "약점"
    sa = by[ChangeType.SLIDE_ADDED][0]
    roles = {(x.text, x.role) for x in sa.anchors}
    assert ("타겟 고객", "neighbor_left") in roles and ("결론", "neighbor_right") in roles
    assert sa.location.slide_index == 5


def test_slide_removed_and_moved(tmp_path):
    a = _deck(tmp_path / "a.pptx")
    prs = Presentation(a)
    ids = prs.slides._sldIdLst
    items = list(ids)
    ids.remove(items[3])  # 타겟 고객 삭제
    ids.remove(items[4])
    ids.insert(1, items[4])  # 결론을 앞으로 이동
    b = tmp_path / "b.pptx"
    prs.save(b)
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS", integ.failures
    types = [c.change_type for c in ch]
    assert ChangeType.SLIDE_REMOVED in types
    assert ChangeType.SLIDE_MOVED in types


def test_paragraph_removed(tmp_path):
    a = _deck(tmp_path / "a.pptx")
    prs = Presentation(a)
    tf = prs.slides[4].placeholders[1].text_frame
    p = tf.paragraphs[0]._p
    p.getparent().remove(p)
    b = tmp_path / "b.pptx"
    prs.save(b)
    ch, _, integ = _diff(a, b)
    assert integ.status == "PASS"
    assert [c.change_type for c in ch] == [ChangeType.TEXT_REMOVED]
    assert ch[0].before == "AI 추천으로 차별화"
