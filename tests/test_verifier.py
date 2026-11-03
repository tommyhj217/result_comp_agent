from __future__ import annotations

from result_comp_agent.agents.grounding import check_edit, locate_quote
from result_comp_agent.agents.verifier_agent import VerifierAgent
from result_comp_agent.code.indexer import CodeIndex
from result_comp_agent.models import Change, ChangeType, CodeRef, Location, ProposedEdit, Recommendation

SRC = '''COLUMNS = [
    "요구사항ID",
    "우선순위",
    "담당자",
]
LEVELS = ["상", "중", "하"]
OTHER = ["상", "중"]


def build(ws):
    ws.append(COLUMNS)
'''


def _idx():
    i = CodeIndex(agent_id="a", commit="c")
    i.add_text_file("agent.py", SRC)
    i.finalize()
    return i


def _change(cid="p-1:C0001"):
    return Change(id=cid, pair_id="p-1", agent_id="a", artifact="xlsx", change_type=ChangeType.COLUMN_ADDED,
                  location=Location(artifact="xlsx", container="c", path="p"), after="리스크",
                  code_refs=[CodeRef(agent_id="a", commit="c", file="agent.py", line_start=3, line_end=4, kind="insertion_point",
                                     match_method="container", anchor="리스크", anchor_role="insertion", matched_text="", score=0.95)])


def _llm_ref(file, line, quote):
    return CodeRef(agent_id="a", commit="c", file=file, line_start=line, line_end=line, kind="llm_location", match_method="llm_quote",
                   anchor="", anchor_role="llm", matched_text=quote, score=0.0)


def test_locate_quote_exact_relocated_missing():
    idx = _idx()
    assert locate_quote(idx, "agent.py", '"우선순위",', 3, 3)[0] == "exact"
    st, s, _ = locate_quote(idx, "agent.py", "ws.append(COLUMNS)", 2, 2)
    assert st == "relocated" and s == 11
    assert locate_quote(idx, "agent.py", "존재하지 않는 코드", 1, 1)[0] == "not_found"
    assert locate_quote(idx, "nope.py", "x", 1, 1)[0] == "no_file"
    # 모델이 줄번호 접두어를 포함해 인용해도 처리
    assert locate_quote(idx, "agent.py", '    3|     "우선순위",', 3, 3)[0] == "exact"


def test_check_edit_unique_ambiguous_syntax():
    idx = _idx()
    ok, note, patch = check_edit(idx, "agent.py", '    "우선순위",\n    "담당자",', '    "우선순위",\n    "리스크",\n    "담당자",')
    assert ok and '+    "리스크",' in patch and patch.startswith("--- a/agent.py")
    ok, note, _ = check_edit(idx, "agent.py", '"상", "중"', '"상"')
    assert not ok and "2회" in note
    ok, note, _ = check_edit(idx, "agent.py", "ws.append(COLUMNS)", "ws.append(COLUMNS")
    assert not ok and "구문 오류" in note
    ok, note, _ = check_edit(idx, "ghost.py", "a", "b")
    assert not ok


def test_verifier_statuses():
    idx = _idx()
    ch = _change()
    changes = {ch.id: ch}
    good = Recommendation(id="a:L001", agent_id="a", title="리스크 열 추가", problem="p", recommendation="r", category="STRUCTURE_CHANGE", source="llm",
                          evidence_change_ids=[ch.id], code_refs=[_llm_ref("agent.py", 3, '"우선순위",')],
                          proposed_edits=[ProposedEdit(file="agent.py", original_snippet='    "우선순위",\n    "담당자",', replacement='    "우선순위",\n    "리스크",\n    "담당자",')])
    wrong_line = Recommendation(id="a:L002", agent_id="a", title="t", problem="p", recommendation="r", category="STRUCTURE_CHANGE", source="llm",
                                evidence_change_ids=[ch.id], code_refs=[_llm_ref("agent.py", 1, '"담당자",')])
    hallucinated = Recommendation(id="a:L003", agent_id="a", title="환각", problem="p", recommendation="r", category="STRUCTURE_CHANGE", source="llm",
                                  evidence_change_ids=["p-9:C9999"], code_refs=[_llm_ref("ghost.py", 10, "def ghost():")])
    fake_code = Recommendation(id="a:L004", agent_id="a", title="t", problem="p", recommendation="r", category="STRUCTURE_CHANGE", source="llm",
                               evidence_change_ids=[ch.id], code_refs=[_llm_ref("agent.py", 3, "validate_schema(rows)")])
    kept, rejected = VerifierAgent().run([good, wrong_line, hallucinated, fake_code], index=idx, changes=changes, n_pairs=1, patterns=[], agent_label="a", notes=[])
    st = {r.id: r.verification.status for r in kept + rejected}
    assert st["a:L001"] == "VERIFIED"
    assert good.proposed_edits[0].verified and good.proposed_edits[0].patch
    assert st["a:L002"] == "VERIFIED" and wrong_line.code_refs[0].line_start == 4  # 줄 번호 보정
    assert st["a:L003"] == "REJECTED" and hallucinated in rejected
    assert st["a:L004"] == "PARTIAL" and fake_code.code_refs == []  # 코드에 없는 인용은 제거
    assert good.confidence > fake_code.confidence
    assert set(good.confidence_breakdown) >= {"evidence(0.4)", "code_grounding(0.3)", "edit_validity(0.1)", "consistency(0.2)"}
