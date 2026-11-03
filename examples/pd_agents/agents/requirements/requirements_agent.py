"""2. 요구사항 정의 단계 — 요구사항 정의서(Excel) 생성 에이전트."""
from __future__ import annotations

import json

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from common.llm_client import call_gpt, llm_available, offline_sample

SHEET_NAME = "요구사항정의서"
COLUMNS = [
    "요구사항ID",
    "구분",
    "요구사항명",
    "상세설명",
    "우선순위",
    "담당자",
]
PRIORITY_VALUES = ["상", "중", "하"]
DEFAULT_OWNER = "기획팀"

SYSTEM_PROMPT = """당신은 제품 기획 PM입니다.
제품 개요와 시장분석 결과를 바탕으로 요구사항 정의서를 작성합니다.
각 요구사항은 구분(기능/비기능), 요구사항명, 상세설명, 우선순위(상/중/하), 담당자를 포함합니다.
상세설명은 1~2문장으로 간결하게 작성하세요.
응답 형식: {"requirements": [{"구분":..., "요구사항명":..., "상세설명":..., "우선순위":..., "담당자":...}]}
JSON 형식으로만 응답하세요."""


def generate_requirements(brief: dict) -> list[dict]:
    if llm_available():
        return call_gpt(SYSTEM_PROMPT, json.dumps(brief, ensure_ascii=False))["requirements"]
    return offline_sample("requirements")["requirements"]


def build_workbook(brief: dict, reqs: list[dict], out_path: str) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws["A1"] = f"{brief['product']} 요구사항 정의서"
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(COLUMNS)
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for i, req in enumerate(reqs, start=1):
        req = {**req, "담당자": req.get("담당자") or DEFAULT_OWNER}
        ws.append([f"REQ-{i:03d}"] + [req.get(col, "") for col in COLUMNS[1:]])
    for col, width in zip("ABCDEF", (12, 8, 18, 50, 8, 12)):
        ws.column_dimensions[col].width = width
    wb.save(out_path)
    return out_path


def run(brief: dict, out_path: str) -> str:
    return build_workbook(brief, generate_requirements(brief), out_path)
