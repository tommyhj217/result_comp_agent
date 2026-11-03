"""두 git ref 사이의 에이전트 코드 변경 분석.

용도
* 사용자 보완 브랜치(예: feature/kim-review) vs 기준 브랜치 → 사용자가 코드에 직접 보완한 사항
* 산출물 원본/수정본을 만든 코드 버전이 다를 때 → 산출물 변경 중 '코드 변경으로 설명되는 것' 식별
"""
from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath
from typing import Optional

from ..diff import normalize as N
from ..models import Change, CodeDiffReport, CodeRef, DiffHunk, FileDiffReport
from .git_source import GitRepo, WORKTREE
from .indexer import STR_LIT_RE, _PyVisitor, _regex_symbols, CODE_EXT

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(text: str) -> list[FileDiffReport]:
    files: list[FileDiffReport] = []
    cur: Optional[FileDiffReport] = None
    hunk: Optional[DiffHunk] = None
    old_no = new_no = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            a, b = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiffReport(path=b, old_path=a if a != b else None, status="modified")
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status = "renamed"
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.path = line[len("rename to "):]
        elif line.startswith("Binary files "):
            cur.status = "binary"
        elif line.startswith("--- ") or line.startswith("+++ "):
            continue
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                hunk = DiffHunk(
                    old_start=int(m.group(1)), old_len=int(m.group(2) or 1), new_start=int(m.group(3)), new_len=int(m.group(4) or 1),
                    symbol=(m.group(5) or "").strip() or None,
                )
                cur.hunks.append(hunk)
                old_no, new_no = hunk.old_start, hunk.new_start
        elif hunk is not None:
            if line.startswith("+"):
                hunk.added.append((new_no, line[1:]))
                cur.added_lines += 1
                new_no += 1
            elif line.startswith("-"):
                hunk.removed.append((old_no, line[1:]))
                cur.removed_lines += 1
                old_no += 1
            elif line.startswith(" "):
                old_no += 1
                new_no += 1
    return files


def _symbols_for(text: str, path: str) -> list[tuple[int, int, str]]:
    ext = PurePosixPath(path).suffix.lower()
    if ext == ".py":
        try:
            v = _PyVisitor(path)
            v.visit(ast.parse(text))
            return v.symbols
        except SyntaxError:
            return []
    if ext in CODE_EXT:
        return _regex_symbols(text.split("\n"))
    return []


def _innermost(symbols, line: int) -> Optional[str]:
    best = None
    for s, e, n in symbols:
        if s <= line <= e and (best is None or e - s < best[1] - best[0]):
            best = (s, e, n)
    return best[2] if best else None


def _literals_in(lines: list[str], text_file: bool) -> list[str]:
    out = []
    for ln in lines:
        found = [m.group("body").strip() for m in STR_LIT_RE.finditer(ln)]
        out.extend(f for f in found if f)
        if text_file and not found and ln.strip():
            out.append(ln.strip().lstrip("-*# ").strip())
    return [x for x in dict.fromkeys(out) if x]


def build_code_diff(repo: GitRepo, agent_id: str, label: str, base_ref: str, head_ref: str, paths: Optional[list[str]]) -> CodeDiffReport:
    try:
        base_c = repo.resolve(base_ref)
        head_c = repo.resolve(head_ref)
        raw = repo.diff(base_c, head_c, paths)
    except Exception as e:
        return CodeDiffReport(agent_id=agent_id, label=label, base_ref=base_ref, base_commit="", head_ref=head_ref, head_commit="", error=str(e))
    files = parse_unified_diff(raw)
    added_lits, removed_lits = [], []
    for f in files:
        if f.status in ("deleted", "binary"):
            continue
        try:
            head_text = repo.read_text(head_c, f.path)
        except Exception:
            head_text = ""
        syms = _symbols_for(N.clean_text(head_text), f.path)
        is_text = PurePosixPath(f.path).suffix.lower() not in {".py"} | CODE_EXT
        for h in f.hunks:
            line = h.added[0][0] if h.added else h.new_start
            sym = _innermost(syms, line)
            if sym:
                h.symbol = sym
            added_lits += _literals_in([t for _, t in h.added], is_text)
            removed_lits += _literals_in([t for _, t in h.removed], is_text)
    return CodeDiffReport(
        agent_id=agent_id, label=label, base_ref=base_ref, base_commit=base_c, head_ref=head_ref,
        head_commit=head_c if head_c != WORKTREE else WORKTREE, files=files,
        literals_added=list(dict.fromkeys(added_lits)), literals_removed=list(dict.fromkeys(removed_lits)),
    )


def find_in_code_diff(report: CodeDiffReport, text: str, role: str) -> list[CodeRef]:
    """코드 diff 의 추가/삭제 줄에서 앵커 텍스트를 찾는다."""
    t = N.collapse_ws(text or "")
    if not t or (len(t) < (2 if N.has_cjk(t) else 4)) or N.norm_for_match(t).isdigit():
        return []
    out: list[CodeRef] = []
    for f in report.files:
        for h in f.hunks:
            for side, lines in (("added", h.added), ("removed", h.removed)):
                for no, ln in lines:
                    if t in N.collapse_ws(ln):
                        out.append(
                            CodeRef(
                                agent_id=report.agent_id,
                                commit=report.head_commit if side == "added" else report.base_commit,
                                file=f.path,
                                line_start=no,
                                line_end=no,
                                symbol=h.symbol,
                                kind="code_change",
                                match_method="diff",
                                anchor=t,
                                anchor_role=role,
                                matched_text=ln.strip()[:300],
                                score=0.9 if side == "added" else 0.85,
                                snippet=("+" if side == "added" else "-") + ln,
                                note=f"[{report.label}] 코드에서 {'추가' if side == 'added' else '삭제'}된 줄",
                            )
                        )
                        break
    return out[:6]


def correlate_changes(changes: list[Change], report: CodeDiffReport) -> int:
    """산출물 변경 중 코드 변경으로 설명되는 것을 연결. 연결된 변경 수를 반환."""
    if report.error:
        return 0
    n = 0
    for ch in changes:
        if ch.format_only:
            continue
        refs: list[CodeRef] = []
        for a in ch.anchors:
            if a.role in ("neighbor_left", "neighbor_right"):
                continue
            refs.extend(find_in_code_diff(report, a.text, a.role))
        if refs:
            seen = set()
            uniq = []
            for r in refs:
                k = (r.file, r.line_start, r.commit)
                if k not in seen:
                    seen.add(k)
                    uniq.append(r)
            ch.code_change_refs.extend(uniq[:4])
            n += 1
    return n
