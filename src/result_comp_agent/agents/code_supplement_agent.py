"""CodeSupplementAgent — 사용자가 에이전트 코드(브랜치)에 직접 보완한 사항 요약.

결정론적 요약(파일·함수·추가 리터럴)을 항상 만들고, GPT 가 가능하면 hunk 별 의도를 요약한다.
LLM 요약은 file + hunk_new_start 가 실제 diff 에 존재할 때만 verified=True.
"""
from __future__ import annotations

import re
from typing import Optional

from ..diff import normalize as N
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import CODE_SUPPLEMENT_SYSTEM
from ..llm.schemas import CODE_SUPPLEMENT_SCHEMA, CodeSupplementM
from ..models import CodeDiffReport, CodeSupplementItem

_PROMPT_HINT = re.compile(r"(?i)prompt|instruction|system|지침|작성|응답|프롬프트|하세요|하십시오")
_VALID_HINT = re.compile(r"(?i)valid|assert|raise|check|검증|if not|error")
_FMT_HINT = re.compile(r"(?i)font|fill|style|color|width|format|bold|서식")


_BARE_STR = re.compile(r"""^\s*["'][^"']+["'],?\s*$""")


def _heuristic_kind(lines: list[str], path: str) -> str:
    text = "\n".join(lines)
    code_lines = [l for l in lines if l.strip()]
    if code_lines and all(_BARE_STR.match(l) for l in code_lines):
        return "schema"
    if _PROMPT_HINT.search(text) or re.search(r"(?i)prompt", path):
        return "prompt"
    if _VALID_HINT.search(text):
        return "validation"
    if _FMT_HINT.search(text):
        return "formatting"
    if re.search(r"""\[\s*["']|["']\s*,\s*["']""", text):
        return "schema"
    return "logic"


class CodeSupplementAgent:
    def __init__(self, llm: Optional[LLMClient], max_chars: int = 40000):
        self.llm = llm
        self.max_chars = max_chars

    def deterministic(self, report: CodeDiffReport) -> list[CodeSupplementItem]:
        out = []
        for f in report.files:
            for h in f.hunks:
                added = [t for _, t in h.added]
                removed = [t for _, t in h.removed]
                lits = [m for m in re.findall(r"""["']([^"']{2,60})["']""", "\n".join(added))][:6]
                summary = f"+{len(added)}/-{len(removed)}줄"
                if lits:
                    summary += " · 추가 문자열: " + ", ".join(lits)
                out.append(
                    CodeSupplementItem(
                        file=f.path, symbol=h.symbol, kind=_heuristic_kind(added, f.path), summary=summary,
                        hunk_new_start=h.new_start, verified=True,
                    )
                )
        return out

    def run(self, report: CodeDiffReport) -> list[CodeSupplementItem]:
        if report.error:
            return []
        det = self.deterministic(report)
        if self.llm is None or not self.llm.active or not det:
            return det
        blocks, size = [], 0
        valid: set[tuple[str, int]] = set()
        for f in report.files:
            for h in f.hunks:
                valid.add((f.path, h.new_start))
                body = "\n".join([f"-{t}" for _, t in h.removed] + [f"+{t}" for _, t in h.added])
                blk = f"### file={f.path} hunk_new_start={h.new_start} symbol={h.symbol or ''}\n{body}"
                if size + len(blk) > self.max_chars:
                    break
                blocks.append(blk)
                size += len(blk)
        user = f"[비교] {report.label}: {report.base_ref} → {report.head_ref}\n\n" + "\n\n".join(blocks)

        def feedback(m: CodeSupplementM) -> list[str]:
            return [f"존재하지 않는 hunk 참조: {i.file}:{i.hunk_new_start}" for i in m.items if (i.file, i.hunk_new_start) not in valid]

        try:
            res = self.llm.structured(
                tier="main", system=CODE_SUPPLEMENT_SYSTEM, user=user, schema_name="code_supplements",
                schema=CODE_SUPPLEMENT_SCHEMA, validate=CodeSupplementM.model_validate, feedback=feedback, purpose=f"code_supplement:{report.agent_id}",
            )
        except LLMError:
            return det
        out = []
        for it in res.items:
            ok = (it.file, it.hunk_new_start) in valid
            out.append(
                CodeSupplementItem(
                    file=it.file, symbol=it.symbol or None, kind=it.kind, summary=N.shorten(it.summary, 200),
                    hunk_new_start=it.hunk_new_start, verified=ok,
                )
            )
        # LLM 이 다루지 않은 hunk 는 결정론적 요약으로 보충
        covered = {(i.file, i.hunk_new_start) for i in out if i.verified}
        out += [d for d in det if (d.file, d.hunk_new_start) not in covered]
        return out
