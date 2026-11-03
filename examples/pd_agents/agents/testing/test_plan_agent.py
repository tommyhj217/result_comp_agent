"""5. 테스트 단계 — 테스트 케이스(Excel) 생성 에이전트."""
from __future__ import annotations

import json

from openpyxl import Workbook
from openpyxl.styles import Font

from common.llm_client import call_gpt, llm_available, offline_sample

SHEET_NAME = "테스트케이스"
COLUMNS = ["TC ID", "요구사항ID", "테스트 시나리오", "사전조건", "기대결과", "우선순위"]

SYSTEM_PROMPT = """당신은 QA 엔지니어입니다.
요구사항 정의서를 바탕으로 요구사항별 테스트 케이스를 작성합니다.
각 테스트 케이스는 요구사항ID, 테스트 시나리오, 사전조건, 기대결과, 우선순위를 포함합니다.
정상 동작 시나리오를 중심으로 요구사항당 1개씩 작성하세요.
응답 형식: {"test_cases": [{"요구사항ID":..., "테스트 시나리오":..., "사전조건":..., "기대결과":..., "우선순위":...}]}
JSON 형식으로만 응답하세요."""


def generate_cases(brief: dict) -> list[dict]:
    if llm_available():
        return call_gpt(SYSTEM_PROMPT, json.dumps(brief, ensure_ascii=False))["test_cases"]
    return offline_sample("test_plan")["test_cases"]


def build_workbook(cases: list[dict], out_path: str) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, tc in enumerate(cases, start=1):
        ws.append([f"TC-{i:03d}"] + [tc.get(col, "") for col in COLUMNS[1:]])
    wb.save(out_path)
    return out_path


def run(brief: dict, out_path: str) -> str:
    return build_workbook(generate_cases(brief), out_path)
