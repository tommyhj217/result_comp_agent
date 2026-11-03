"""1. 기획 단계 — 시장분석 보고서(PPT) 생성 에이전트."""
from __future__ import annotations

import json

from pptx import Presentation
from pptx.util import Inches, Pt

from common.llm_client import call_gpt, llm_available, offline_sample

REPORT_TITLE = "시장분석 보고서"
SECTIONS = [
    "시장 개요",
    "경쟁사 분석",
    "타겟 고객",
    "결론 및 제언",
]
COMPETITOR_COLUMNS = ["경쟁사", "강점", "약점", "가격대"]

SYSTEM_PROMPT = """당신은 시장조사 전문 컨설턴트입니다.
제품 개요를 바탕으로 경영진 보고용 시장분석 보고서 내용을 작성합니다.
섹션: 시장 개요, 경쟁사 분석, 타겟 고객, 결론 및 제언.
각 섹션은 핵심 bullet 3~4개로 간결하게 작성하세요.
경쟁사 분석은 경쟁사별 강점, 약점, 가격대를 표로 작성하세요.
응답 형식: {"sections": {"섹션명": ["bullet", ...]}, "competitors": [{"경쟁사":..., "강점":..., "약점":..., "가격대":...}]}
JSON 형식으로만 응답하세요."""


def generate_content(brief: dict) -> dict:
    if llm_available():
        return call_gpt(SYSTEM_PROMPT, json.dumps(brief, ensure_ascii=False))
    return offline_sample("market_analysis")


def _bullet_slide(prs: Presentation, title: str, bullets: list[str]) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    tf = slide.placeholders[1].text_frame
    tf.text = bullets[0]
    for b in bullets[1:]:
        tf.add_paragraph().text = b


def _competitor_slide(prs: Presentation, rows: list[dict]) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "경쟁사 분석"
    table = slide.shapes.add_table(len(rows) + 1, len(COMPETITOR_COLUMNS), Inches(0.5), Inches(1.8), Inches(9), Inches(0.5) * (len(rows) + 1)).table
    for c, name in enumerate(COMPETITOR_COLUMNS):
        table.cell(0, c).text = name
    for r, row in enumerate(rows, start=1):
        for c, name in enumerate(COMPETITOR_COLUMNS):
            table.cell(r, c).text = str(row.get(name, ""))
            table.cell(r, c).text_frame.paragraphs[0].runs[0].font.size = Pt(14)


def build_deck(brief: dict, content: dict, out_path: str) -> str:
    prs = Presentation()
    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = REPORT_TITLE
    cover.placeholders[1].text = f"{brief['product']} · {brief.get('team', '기획팀')}"
    for section in SECTIONS:
        if section == "경쟁사 분석":
            _competitor_slide(prs, content["competitors"])
        else:
            _bullet_slide(prs, section, content["sections"][section])
    prs.save(out_path)
    return out_path


def run(brief: dict, out_path: str) -> str:
    return build_deck(brief, generate_content(brief), out_path)
