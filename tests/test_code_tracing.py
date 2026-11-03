from __future__ import annotations

import subprocess

from result_comp_agent.code.code_diff import build_code_diff, find_in_code_diff, parse_unified_diff
from result_comp_agent.code.git_source import WORKTREE, GitRepo
from result_comp_agent.code.indexer import CodeIndex
from result_comp_agent.code.tracer import CodeTracer
from result_comp_agent.models import Anchor, Change, ChangeType, Location

AGENT = '''"""요구사항 에이전트"""
SHEET_NAME = "요구사항정의서"
COLUMNS = [
    "요구사항ID",
    "구분",
    "상세설명",
    "우선순위",
    "담당자",
]
DEFAULT_OWNER = "기획팀"
SYSTEM_PROMPT = """당신은 PM입니다.
각 요구사항은 요구사항ID, 구분, 상세설명, 우선순위, 담당자를 포함합니다.
상세설명은 1~2문장으로 간결하게 작성하세요.
JSON 으로만 응답하세요."""


def build(ws, rows):
    ws.title = SHEET_NAME
    ws.append(COLUMNS)
'''


def _index(src=AGENT, path="agents/req.py"):
    idx = CodeIndex(agent_id="req", commit="c0ffee")
    idx.add_text_file(path, src)
    idx.finalize()
    return idx


def _change(ct, anchors, **kw):
    return Change(id="x:C0001", pair_id="x", agent_id="req", artifact="xlsx", change_type=ct,
                  location=Location(artifact="xlsx", container="c", path="p"), anchors=[Anchor(text=t, role=r) for t, r in anchors], **kw)


def test_index_kinds_containers_symbols():
    idx = _index()
    by_val = {l.value: l for l in idx.literals}
    assert by_val["요구사항ID"].container == by_val["담당자"].container
    assert by_val["요구사항ID"].symbol == "COLUMNS"
    assert by_val["기획팀"].symbol == "DEFAULT_OWNER"
    assert any(l.kind == "prompt" and l.symbol == "SYSTEM_PROMPT" for l in idx.literals)
    assert by_val["요구사항 에이전트"].kind == "docstring"


def test_insertion_point_between_neighbors():
    tr = CodeTracer(_index())
    ch = _change(ChangeType.COLUMN_ADDED, [("리스크", "new_column"), ("우선순위", "neighbor_left"), ("담당자", "neighbor_right")],
                 after="리스크", detail={"sibling_headers": ["요구사항ID", "구분", "상세설명", "우선순위", "담당자"]})
    refs = tr.trace(ch)
    ip = next(r for r in refs if r.kind == "insertion_point")
    assert (ip.file, ip.line_start, ip.line_end) == ("agents/req.py", 7, 8)
    assert ip.match_method == "container" and ip.score >= 0.9


def test_hardcoded_value_ranks_first_and_prompt_is_linked():
    tr = CodeTracer(_index())
    ch = _change(ChangeType.CELL_MODIFIED, [("요구사항정의서", "sheet"), ("담당자", "column"), ("기획팀", "value"), ("모바일팀", "new_value")],
                 before="기획팀", after="모바일팀")
    refs = tr.trace(ch)
    assert refs[0].anchor_role == "value" and refs[0].line_start == 10 and refs[0].symbol == "DEFAULT_OWNER"
    assert any(r.kind == "prompt" for r in refs)


def test_untraceable_anchor_returns_nothing():
    tr = CodeTracer(_index())
    ch = _change(ChangeType.CELL_MODIFIED, [("존재하지않는열", "column")])
    assert tr.trace(ch) == []


def test_substring_in_prompt_line_located():
    idx = _index()
    m = [x for x in idx.find("상세설명", substring=True, fuzzy=False, limit=10) if x.lit.kind == "prompt"]
    assert m and m[0].line == 12


def _git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_git_repo_read_and_diff(tmp_path):
    repo = tmp_path / "r"
    (repo / "agents").mkdir(parents=True)
    (repo / "agents" / "req.py").write_text(AGENT, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "agents" / "req.py").write_text(AGENT.replace('    "우선순위",\n', '    "우선순위",\n    "리스크",\n'), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "add risk")
    _git(repo, "checkout", "-q", "main")
    g = GitRepo(str(repo), tmp_path / "cache")
    c_main = g.resolve("main")
    assert len(c_main) == 40
    assert g.list_files(c_main, ["agents"]) == ["agents/req.py"]
    idx = CodeIndex.build(g, g.resolve("feature/x"), ["agents"], "req")
    assert any(l.value == "리스크" for l in idx.literals)
    rep = build_code_diff(g, "req", "branch", "main", "feature/x", ["agents"])
    assert rep.error is None and len(rep.files) == 1
    h = rep.files[0].hunks[0]
    assert h.symbol == "COLUMNS" and h.added[0][1].strip() == '"리스크",'
    assert "리스크" in rep.literals_added
    refs = find_in_code_diff(rep, "리스크", "new_column")
    assert refs and refs[0].line_start == 8 and "추가" in refs[0].note
    # 작업 트리 모드
    assert g.resolve(WORKTREE) == WORKTREE


def test_remote_style_clone(tmp_path):
    """URL(file://)로 지정하면 bare clone 후 읽는다."""
    repo = tmp_path / "origin"
    repo.mkdir()
    (repo / "a.py").write_text('X = "값"\n', encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    g = GitRepo(f"file://{repo}", tmp_path / "cache")
    c = g.resolve("main")
    assert g.read_text(c, "a.py").strip() == 'X = "값"'


def test_parse_unified_diff_counts():
    txt = """diff --git a/x.py b/x.py
index 1..2 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,4 @@ def f():
 a
-b
+B
+c
 d
"""
    files = parse_unified_diff(txt)
    assert files[0].added_lines == 2 and files[0].removed_lines == 1
    h = files[0].hunks[0]
    assert h.added == [(2, "B"), (3, "c")] and h.removed == [(2, "b")]


def test_plain_directory_mode(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "agent.py").write_text('COLS = ["가", "나"]\n', encoding="utf-8")
    g = GitRepo(str(tmp_path / "p"))
    assert g.mode == "plain" and g.resolve("main") == WORKTREE
    idx = CodeIndex.build(g, WORKTREE, [], "x")
    assert {l.value for l in idx.literals} >= {"가", "나"}
