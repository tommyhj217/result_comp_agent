"""근거 검증 공용 함수: 코드 인용문 위치 확인, 수정안(edit) 적용 가능성 확인 및 패치 생성."""
from __future__ import annotations

import ast
import difflib
import re
from typing import Optional

from ..code.indexer import CodeIndex
from ..diff import normalize as N

_LINENO_PREFIX = re.compile(r"^\s*\d+\s*\|\s?", re.M)


def clean_quote(q: str) -> str:
    return _LINENO_PREFIX.sub("", N.clean_text(q or "")).strip("\n")


def locate_quote(index: CodeIndex, file: str, quote: str, hint_start: int, hint_end: int, slack: int = 3) -> tuple[str, int, int]:
    """반환: (status, line_start, line_end)
    status: exact(힌트 범위에서 발견) | relocated(다른 줄에서 발견) | not_found | no_file | empty | template"""
    if file in index.binary_files:
        q = N.collapse_ws(clean_quote(quote))
        ok = any(l.file == file and q and q in l.clean for l in index.literals)
        return ("template" if ok else "not_found", 0, 0)
    lines = index.files.get(file)
    if lines is None:
        return ("no_file", hint_start, hint_end)
    q_lines = [N.collapse_ws(x) for x in clean_quote(quote).split("\n")]
    q_lines = [x for x in q_lines if x]
    if not q_lines or (len(q_lines) == 1 and len(q_lines[0]) < 3):
        return ("empty", hint_start, hint_end)
    norm = [N.collapse_ws(x) for x in lines]
    n = len(q_lines)

    def match_at(i: int) -> bool:
        if i < 0 or i + n > len(norm):
            return False
        return all(q_lines[k] in norm[i + k] for k in range(n))

    lo = max(0, (hint_start or 1) - 1 - slack)
    hi = min(len(norm) - 1, max(hint_end or hint_start or 1, hint_start or 1) - 1 + slack)
    for i in range(lo, hi + 1):
        if match_at(i):
            return ("exact", i + 1, i + n)
    found = [i for i in range(len(norm)) if match_at(i)]
    if found:
        i = min(found, key=lambda x: abs(x + 1 - (hint_start or 1)))
        return ("relocated", i + 1, i + n)
    return ("not_found", hint_start, hint_end)


def check_edit(index: CodeIndex, file: str, original: str, replacement: str) -> tuple[bool, str, Optional[str]]:
    """수정안 적용 가능성 검증. 반환: (verified, note, unified_patch)"""
    text = index.file_text(file)
    if text is None:
        return False, "파일이 인덱스에 없음(존재하지 않거나 텍스트가 아님)", None
    orig = clean_quote(original)
    repl = _LINENO_PREFIX.sub("", N.clean_text(replacement or ""))
    if not orig.strip():
        return False, "원본 스니펫이 비어 있음", None
    base = text
    note = ""
    cnt = base.count(orig)
    if cnt == 0:
        base_rs = "\n".join(l.rstrip() for l in text.split("\n"))
        orig_rs = "\n".join(l.rstrip() for l in orig.split("\n"))
        if base_rs.count(orig_rs) == 1:
            base, orig, cnt, note = base_rs, orig_rs, 1, "후행 공백 정규화 후 일치; "
            repl = "\n".join(l.rstrip() for l in repl.split("\n"))
    if cnt != 1:
        return False, f"원본 스니펫이 파일에서 {cnt}회 발견됨(정확히 1회여야 적용 가능)", None
    new_text = base.replace(orig, repl, 1)
    if new_text == base:
        return False, "수정 전후가 동일함", None
    if file.endswith(".py"):
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            return False, f"{note}적용 시 Python 구문 오류 ({e.lineno}행: {e.msg})", None
    patch = "".join(
        difflib.unified_diff(base.splitlines(True), new_text.splitlines(True), fromfile=f"a/{file}", tofile=f"b/{file}", n=2)
    )
    return True, note + "원본 스니펫 1회 일치, 패치 생성", patch
