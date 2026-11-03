"""에이전트 코드 인덱스.

산출물의 구조(시트명, 컬럼명, 슬라이드 제목, 섹션명)는 대부분 코드 안의 문자열 리터럴·프롬프트·템플릿 파일에서 온다.
이 인덱스는 특정 커밋의 코드에서 다음을 수집한다.

* Python: AST 로 문자열 리터럴 / f-string 템플릿 / 프롬프트(이름·길이 기준) / docstring, 소속 함수(qualname),
  같은 리스트·튜플·딕셔너리·함수호출 인자에 속한 '형제 리터럴' 컨테이너 ID
* 기타 코드(js/ts/java/...): 따옴표 문자열 리터럴 + 줄 단위 텍스트
* 설정/프롬프트 파일(md/txt/yaml/json/j2/prompt): 줄 단위 텍스트, 연속 블록 = 컨테이너
* 템플릿(.xlsx/.pptx): 셀/문단 텍스트 (sub_location 에 시트!셀 / 슬라이드 위치 기록)

매칭은 결정론적이며 방식(exact > normalized > substring > fuzzy)별로 점수가 다르다.
"""
from __future__ import annotations

import ast
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Optional

from ..diff import normalize as N
from .git_source import GitRepo

PY_EXT = {".py"}
CODE_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".cs", ".rb", ".php", ".scala", ".swift", ".rs"}
TEXT_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".j2", ".jinja", ".jinja2", ".prompt", ".tmpl", ".tpl", ".html", ".xml", ".csv", ".sql"}
TEMPLATE_EXT = {".xlsx", ".xlsm", ".pptx"}
MAX_TEXT_BYTES = 1_500_000
MAX_TEMPLATE_BYTES = 30_000_000

PROMPT_NAME_RE = re.compile(r"(?i)(prompt|template|instruction|system|message|guideline|persona|지침|프롬프트|지시)")
STR_LIT_RE = re.compile(r"""(?P<q>["'`])(?P<body>(?:\\.|(?!(?P=q)).){1,500})(?P=q)""")
YAML_KV_RE = re.compile(r"""^\s*(?:-\s+)?(?:[\w\-\s가-힣]+:\s+)?["']?(?P<v>[^"'#]+?)["']?\s*$""")
JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+(?P<f>\w+)|class\s+(?P<c>\w+)|(?:const|let|var)\s+(?P<v>\w+)\s*=\s*(?:async\s*)?(?:\(|function))"
    r"|^\s*(?:public|private|protected|static|\s)+[\w<>\[\],\s]+\s+(?P<m>\w+)\s*\([^;]*$"
)

KIND_WEIGHT = {
    "literal": 1.0,
    "template": 1.0,
    "fstring": 0.95,
    "prompt": 0.95,
    "text_line": 0.9,
    "docstring": 0.6,
}
SOFT_KINDS = {"prompt", "text_line", "docstring", "fstring"}


@dataclass
class Literal:
    file: str
    line_start: int
    line_end: int
    value: str
    kind: str
    symbol: Optional[str] = None
    container: Optional[str] = None
    sub_location: Optional[str] = None
    norm: str = ""
    clean: str = ""


@dataclass
class Match:
    lit: Literal
    method: str
    score: float
    line: int


@dataclass
class CodeIndex:
    agent_id: str
    commit: str
    files: dict[str, list[str]] = field(default_factory=dict)
    binary_files: set[str] = field(default_factory=set)
    literals: list[Literal] = field(default_factory=list)
    symbols: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    _by_value: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _by_norm: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _cache: dict[tuple, list[Match]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 빌드
    @classmethod
    def build(cls, repo: GitRepo, commit: str, paths: Optional[list[str]], agent_id: str, max_files: int = 3000) -> "CodeIndex":
        idx = cls(agent_id=agent_id, commit=commit)
        files = repo.list_files(commit, paths)
        for f in files[:max_files]:
            ext = PurePosixPath(f).suffix.lower()
            if ext not in PY_EXT | CODE_EXT | TEXT_EXT | TEMPLATE_EXT:
                continue
            try:
                data = repo.read_bytes(commit, f)
            except Exception as e:  # pragma: no cover
                idx.errors.append(f"{f}: {e}")
                continue
            if ext in TEMPLATE_EXT:
                if len(data) <= MAX_TEMPLATE_BYTES:
                    idx.add_template(f, data)
                continue
            if len(data) > MAX_TEXT_BYTES:
                idx.errors.append(f"{f}: 파일이 너무 큼 ({len(data)} bytes) - 건너뜀")
                continue
            idx.add_text_file(f, data.decode("utf-8", "replace"))
        if len(files) > max_files:
            idx.errors.append(f"파일 수 {len(files)} 중 {max_files}개만 인덱싱")
        idx.finalize()
        return idx

    def add_text_file(self, path: str, text: str) -> None:
        text = N.clean_text(text)
        lines = text.split("\n")
        self.files[path] = lines
        ext = PurePosixPath(path).suffix.lower()
        if ext in PY_EXT:
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                self.errors.append(f"{path}: Python 구문 오류 {e.lineno}행 - 줄 단위로 인덱싱")
                self._index_lines(path, lines, code=True)
                return
            v = _PyVisitor(path)
            v.visit(tree)
            self.literals.extend(v.out)
            self.symbols[path] = v.symbols
        elif ext in CODE_EXT:
            self._index_lines(path, lines, code=True)
            self.symbols[path] = _regex_symbols(lines)
        else:
            self._index_lines(path, lines, code=False)

    def _index_lines(self, path: str, lines: list[str], code: bool) -> None:
        block = 0
        prev_blank = True
        for no, raw in enumerate(lines, start=1):
            s = raw.strip()
            if not s:
                prev_blank = True
                continue
            if prev_blank:
                block += 1
            prev_blank = False
            container = f"{path}#block{block}"
            self.literals.append(Literal(path, no, no, s, "text_line", None, container))
            for m in STR_LIT_RE.finditer(raw):
                body = m.group("body").strip()
                if body:
                    self.literals.append(Literal(path, no, no, body, "literal", None, f"{path}:L{no}" if code else container))
            if not code:
                m = YAML_KV_RE.match(raw)
                if m:
                    v = m.group("v").strip()
                    if v and v != s:
                        self.literals.append(Literal(path, no, no, v, "literal", None, container))

    def add_template(self, path: str, data: bytes) -> None:
        self.binary_files.add(path)
        ext = PurePosixPath(path).suffix.lower()
        try:
            if ext in (".xlsx", ".xlsm"):
                from openpyxl import load_workbook

                wb = load_workbook(io.BytesIO(data), data_only=False)
                for ws in wb.worksheets:
                    self.literals.append(Literal(path, 0, 0, ws.title, "template", None, f"{path}#sheets", sub_location=f"시트명 '{ws.title}'"))
                    for row in ws.iter_rows():
                        for cell in row:
                            if isinstance(cell.value, str) and cell.value.strip():
                                self.literals.append(
                                    Literal(path, 0, 0, cell.value.strip(), "template", None, f"{path}#{ws.title}!row{cell.row}", sub_location=f"{ws.title}!{cell.coordinate}")
                                )
            else:
                from pptx import Presentation

                prs = Presentation(io.BytesIO(data))
                for si, slide in enumerate(prs.slides, start=1):
                    for shp in slide.shapes:
                        if getattr(shp, "has_text_frame", False) and shp.has_text_frame:
                            for p in shp.text_frame.paragraphs:
                                t = p.text.strip()
                                if t:
                                    self.literals.append(Literal(path, 0, 0, t, "template", None, f"{path}#slide{si}", sub_location=f"슬라이드 {si} / {shp.name}"))
                for layout in prs.slide_layouts:
                    self.literals.append(Literal(path, 0, 0, layout.name, "template", None, f"{path}#layouts", sub_location=f"레이아웃 '{layout.name}'"))
        except Exception as e:
            self.errors.append(f"{path}: 템플릿 읽기 실패 {e}")

    def finalize(self) -> None:
        self._by_value.clear()
        self._by_norm.clear()
        for i, lit in enumerate(self.literals):
            lit.clean = N.collapse_ws(lit.value)
            lit.norm = N.norm_for_match(lit.value)
            if lit.symbol is None and lit.line_start:
                lit.symbol = self.enclosing_symbol(lit.file, lit.line_start)
            self._by_value[lit.clean].append(i)
            if lit.norm:
                self._by_norm[lit.norm].append(i)
        self._cache.clear()

    # ------------------------------------------------------------------ 조회
    def has_file(self, file: str) -> bool:
        return file in self.files or file in self.binary_files

    def line_count(self, file: str) -> int:
        return len(self.files.get(file, []))

    def get_lines(self, file: str, start: int, end: int) -> list[str]:
        lines = self.files.get(file, [])
        return lines[max(0, start - 1) : max(0, min(end, len(lines)))]

    def file_text(self, file: str) -> Optional[str]:
        lines = self.files.get(file)
        return None if lines is None else "\n".join(lines)

    def enclosing_symbol(self, file: str, line: int) -> Optional[str]:
        best = None
        for s, e, name in self.symbols.get(file, []):
            if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
                best = (s, e, name)
        return best[2] if best else None

    def symbol_range(self, file: str, name: str) -> Optional[tuple[int, int]]:
        for s, e, n in self.symbols.get(file, []):
            if n == name:
                return (s, e)
        return None

    def snippet(self, file: str, start: int, end: int, context: int = 2, max_lines: int = 40) -> str:
        lines = self.files.get(file)
        if not lines or start <= 0:
            return ""
        a = max(1, start - context)
        b = min(len(lines), end + context, a + max_lines - 1)
        return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))

    def prompts(self, limit: int = 30) -> list[Literal]:
        seen = set()
        out = []
        for lit in self.literals:
            is_prompt_file = lit.kind == "text_line" and re.search(r"(?i)prompt|프롬프트|instruction", lit.file)
            if lit.kind == "prompt" or is_prompt_file:
                k = (lit.file, lit.line_start) if lit.kind == "prompt" else (lit.file, "file")
                if k in seen:
                    continue
                seen.add(k)
                out.append(lit)
        return out[:limit]

    def _locate_line(self, lit: Literal, text: str) -> int:
        lines = self.files.get(lit.file)
        if not lines or lit.line_start <= 0:
            return lit.line_start
        probe = text[:30]
        for ln in range(lit.line_start, min(lit.line_end, len(lines)) + 1):
            if probe in lines[ln - 1]:
                return ln
        return lit.line_start

    def find(self, anchor: str, *, substring: bool = True, fuzzy: bool = True, limit: int = 6) -> list[Match]:
        key = (anchor, substring, fuzzy, limit)
        if key in self._cache:
            return self._cache[key]
        res = self._find(anchor, substring, fuzzy, limit)
        self._cache[key] = res
        return res

    def _find(self, anchor: str, substring: bool, fuzzy: bool, limit: int) -> list[Match]:
        a = N.collapse_ws(N.clean_text(anchor or ""))
        an = N.norm_for_match(a)
        if not an or an.isdigit():
            return []
        cjk = N.has_cjk(a)
        min_sub = 2 if cjk else 4
        out: dict[tuple, Match] = {}

        def put(m: Match) -> None:
            k = (m.lit.file, m.line, m.lit.sub_location)
            if k not in out or out[k].score < m.score:
                out[k] = m

        for i in self._by_value.get(a, []):
            lit = self.literals[i]
            put(Match(lit, "exact", round(1.0 * KIND_WEIGHT.get(lit.kind, 0.8), 4), lit.line_start))
        for i in self._by_norm.get(an, []):
            lit = self.literals[i]
            if lit.clean != a:
                put(Match(lit, "normalized", round(0.9 * KIND_WEIGHT.get(lit.kind, 0.8), 4), lit.line_start))
        if substring and len(an) >= min_sub:
            short_pen = 0.85 if len(an) <= 3 else 1.0
            for lit in self.literals:
                if lit.norm == an or len(lit.norm) <= len(an):
                    continue
                base = 0.8 if lit.kind in SOFT_KINDS else 0.6
                if a in lit.clean:
                    put(Match(lit, "substring", round(base * short_pen * KIND_WEIGHT.get(lit.kind, 0.8), 4), self._locate_line(lit, a)))
                elif an in lit.norm:
                    put(Match(lit, "substring", round((base - 0.05) * short_pen * KIND_WEIGHT.get(lit.kind, 0.8), 4), lit.line_start))
        if fuzzy and 3 <= len(an) <= 60:
            for lit in self.literals:
                if lit.kind not in ("literal", "template", "fstring") or not lit.norm or lit.norm == an:
                    continue
                if not (0.7 <= len(lit.norm) / len(an) <= 1.4):
                    continue
                sm = SequenceMatcher(None, an, lit.norm, autojunk=False)
                if sm.quick_ratio() < 0.85:
                    continue
                r = sm.ratio()
                if r >= 0.85:
                    put(Match(lit, "fuzzy", round(0.7 * r * KIND_WEIGHT.get(lit.kind, 0.8), 4), lit.line_start))
        res = sorted(out.values(), key=lambda m: (-m.score, m.lit.file, m.line))
        return res[:limit]

    def find_schema_container(self, names: list[str], min_hits: int = 2) -> Optional[dict]:
        """여러 이름(예: 기존 컬럼 헤더 전체)이 함께 정의된 코드 위치(같은 리스트/딕셔너리/블록)를 찾는다."""
        names = [n for n in dict.fromkeys(N.collapse_ws(x) for x in names if x) if N.norm_for_match(n)]
        if len(names) < min_hits:
            return None
        hits: dict[str, dict[str, Literal]] = defaultdict(dict)
        for nm in names:
            for m in self.find(nm, substring=False, fuzzy=False, limit=50):
                c = m.lit.container or f"{m.lit.file}:L{m.line}"
                hits[c].setdefault(nm, m.lit)
        best, best_key = None, None
        for c, d in hits.items():
            if len(d) < min_hits:
                continue
            lits = list(d.values())
            span = max(l.line_end for l in lits) - min(l.line_start for l in lits)
            key = (len(d), -span)
            if best_key is None or key > best_key:
                best, best_key = c, key
        if best is None:
            return None
        lits = hits[best]
        lst = list(lits.values())
        return {
            "container": best,
            "file": lst[0].file,
            "line_start": min(l.line_start for l in lst),
            "line_end": max(l.line_end for l in lst),
            "coverage": round(len(lits) / len(names), 4),
            "matched": list(lits.keys()),
            "literals": lits,
            "sub_location": lst[0].sub_location,
        }


def _regex_symbols(lines: list[str]) -> list[tuple[int, int, str]]:
    """비 Python 코드의 근사 심볼 범위 (다음 심볼 직전까지)."""
    starts = []
    for no, line in enumerate(lines, start=1):
        m = JS_SYMBOL_RE.match(line)
        if m:
            name = m.group("f") or m.group("c") or m.group("v") or m.group("m")
            if name and name not in ("if", "for", "while", "switch", "return", "catch"):
                starts.append((no, name))
    out = []
    for k, (no, name) in enumerate(starts):
        end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines)
        out.append((no, end, name + " (근사)"))
    return out


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, file: str):
        self.file = file
        self.scope: list[str] = []
        self.names: list[str] = []
        self.container: dict[int, str] = {}
        self.docstrings: set[int] = set()
        self.out: list[Literal] = []
        self.symbols: list[tuple[int, int, str]] = []
        self.fn_depth = 0

    def _qual(self) -> Optional[str]:
        return ".".join(self.scope) or None

    def _mark_doc(self, node) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            self.docstrings.add(id(body[0].value))

    def visit_Module(self, node):
        self._mark_doc(node)
        self.generic_visit(node)

    def _scoped(self, node):
        self.symbols.append((node.lineno, node.end_lineno or node.lineno, ".".join(self.scope + [node.name])))
        self._mark_doc(node)
        is_fn = not isinstance(node, ast.ClassDef)
        self.scope.append(node.name)
        self.fn_depth += int(is_fn)
        self.generic_visit(node)
        self.fn_depth -= int(is_fn)
        self.scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    @staticmethod
    def _tname(t) -> str:
        if isinstance(t, ast.Name):
            return t.id
        if isinstance(t, ast.Attribute):
            return t.attr
        if isinstance(t, ast.Subscript):
            return _PyVisitor._tname(t.value)
        if isinstance(t, (ast.Tuple, ast.List)):
            return " ".join(_PyVisitor._tname(e) for e in t.elts)
        return ""

    def _with_name(self, name: str, node):
        self.names.append(name)
        self.generic_visit(node)
        self.names.pop()

    def _const_symbol(self, name: str, node) -> None:
        # 함수 밖(모듈/클래스) 상수 정의를 심볼로 기록 → 리터럴 위치를 'COLUMNS' 처럼 읽기 쉽게
        if name and self.fn_depth == 0 and isinstance(getattr(node, "value", None), (ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Constant, ast.JoinedStr, ast.Call)):
            self.symbols.append((node.lineno, node.end_lineno or node.lineno, ".".join(self.scope + [name.split(" ")[0]])))

    def visit_Assign(self, node):
        name = " ".join(self._tname(t) for t in node.targets)
        self._const_symbol(name, node)
        self._with_name(name, node)

    def visit_AnnAssign(self, node):
        name = self._tname(node.target)
        self._const_symbol(name, node)
        self._with_name(name, node)

    def visit_keyword(self, node):
        self._with_name(node.arg or "", node)

    def _mark_container(self, node, children) -> None:
        cid = f"{self.file}:{node.lineno}:{node.col_offset}"
        for ch in children:
            if ch is not None:
                self.container[id(ch)] = cid

    def visit_List(self, node):
        self._mark_container(node, node.elts)
        self.generic_visit(node)

    visit_Tuple = visit_List
    visit_Set = visit_List

    def visit_Dict(self, node):
        self._mark_container(node, list(node.keys) + list(node.values))
        self.generic_visit(node)

    def visit_Call(self, node):
        self._mark_container(node, list(node.args) + [k.value for k in node.keywords])
        self.generic_visit(node)

    def _is_prompt(self, s: str) -> bool:
        name = self.names[-1] if self.names else ""
        return bool(PROMPT_NAME_RE.search(name)) and len(s) >= 20 or len(s) >= 200 or s.count("\n") >= 3

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value.strip():
            if id(node) in self.docstrings:
                kind = "docstring"
            else:
                kind = "prompt" if self._is_prompt(node.value) else "literal"
            self.out.append(
                Literal(self.file, node.lineno, node.end_lineno or node.lineno, node.value.strip(), kind, self._qual(), self.container.get(id(node)))
            )

    def visit_JoinedStr(self, node):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                try:
                    parts.append("{" + ast.unparse(v.value) + "}")
                except Exception:  # pragma: no cover
                    parts.append("{…}")
                self.visit(v.value)
        tmpl = "".join(parts).strip()
        if tmpl:
            kind = "prompt" if self._is_prompt(tmpl) else "fstring"
            self.out.append(Literal(self.file, node.lineno, node.end_lineno or node.lineno, tmpl, kind, self._qual(), self.container.get(id(node))))
