"""플러그인 예시 — 부족한 기능을 직접 코딩해 '툴'로 추가하는 방법.

사용:
  config.yaml →  plugins: [../examples/plugins/sample_plugin.py]     (또는 패키지 모듈 경로)
  환경변수     →  RCA_PLUGINS=./examples/plugins/sample_plugin.py
  명령행       →  rca run -c config.yaml --plugin ./examples/plugins/sample_plugin.py
  확인         →  rca tools -c config.yaml

1) 문서 형식 핸들러: JSON 산출물(.json) 비교
   - 흐름형 핸들러(FlowDocHandler)를 상속해 extract() 만 구현하면 diff·무결성 검증·GPT 조회(outline)가 자동 제공
   - 최상위 키를 '섹션(제목)', 각 말단 값을 'path: value' 문단으로 표현 → 값 수정/항목 추가·삭제를 정확히 추적
2) GPT 도구: summarize_changes_by_location
   - GPT 가 분석 중 호출해 '어느 시트/열/섹션에 어떤 보완이 몰려 있는지' 집계를 받아볼 수 있음
"""
from __future__ import annotations

import json
from collections import Counter

from result_comp_agent.diff.flow_diff import Block, FlowDoc
from result_comp_agent.documents import FlowDocHandler, register_handler
from result_comp_agent.extractors.excel import file_sha256
from result_comp_agent.tools import register_tool


class JsonHandler(FlowDocHandler):
    name = "json"
    artifact = "json"
    extensions = (".json",)
    description = "JSON 산출물: 최상위 키=섹션, 말단 값=경로별 문단"

    def extract(self, path: str) -> FlowDoc:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        doc = FlowDoc(path=str(path), sha256=file_sha256(path))

        def walk(node, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{prefix}[{i}]")
            else:
                doc.blocks.append(Block(kind="para", text=f"{prefix}: {json.dumps(node, ensure_ascii=False)}"))

        if isinstance(data, dict):
            for k, v in data.items():
                doc.blocks.append(Block(kind="heading", text=str(k), level=1))
                walk(v, str(k))
        else:
            walk(data, "$")
        return doc


register_handler(JsonHandler())


@register_tool(
    "summarize_changes_by_location",
    "변경을 위치(시트·열 / 슬라이드 / 섹션)와 보완 유형별로 집계한다. 보완이 집중된 곳을 찾을 때 사용.",
    {"type": "object", "properties": {"top": {"type": "integer"}}},
)
def summarize_changes_by_location(ctx, top: int = 15):
    counter: Counter = Counter()
    for c in ctx.changes.values():
        if c.format_only:
            continue
        loc = c.location
        where = loc.sheet and f"{loc.sheet}/{loc.column_label or '-'}" or loc.slide_title or loc.section or loc.path
        counter[(where, c.category or "?")] += 1
    return {"top": [{"location": w, "category": cat, "count": n} for (w, cat), n in counter.most_common(max(1, min(int(top or 15), 50)))]}
