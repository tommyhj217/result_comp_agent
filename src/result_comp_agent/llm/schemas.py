"""LLM 구조화 출력 스키마 (Azure OpenAI Structured Outputs strict 모드 호환) + 검증용 pydantic 모델."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..analysis.classifier import CATEGORIES

CATEGORY_ENUM = list(CATEGORIES.keys())


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required or list(props.keys()), "properties": props}


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}

SUPPLEMENT_SCHEMA = _obj(
    {
        "items": {
            "type": "array",
            "items": _obj(
                {
                    "change_id": STR,
                    "category": {"type": "string", "enum": CATEGORY_ENUM},
                    "user_intent": STR,
                    "agent_gap": STR,
                    "confidence": NUM,
                }
            ),
        }
    }
)

IMPROVEMENT_SCHEMA = _obj(
    {
        "recommendations": {
            "type": "array",
            "items": _obj(
                {
                    "title": STR,
                    "problem": STR,
                    "recommendation": STR,
                    "category": {"type": "string", "enum": CATEGORY_ENUM},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence_change_ids": {"type": "array", "items": STR},
                    "based_on": {"type": "array", "items": STR},
                    "code_locations": {
                        "type": "array",
                        "items": _obj({"file": STR, "line_start": INT, "line_end": INT, "quote": STR}),
                    },
                    "proposed_edits": {
                        "type": "array",
                        "items": _obj({"file": STR, "original_snippet": STR, "replacement": STR, "explanation": STR}),
                    },
                    "expected_effect": STR,
                }
            ),
        }
    }
)

CROSSCHECK_SCHEMA = _obj(
    {
        "results": {
            "type": "array",
            "items": _obj(
                {
                    "rec_id": STR,
                    "verdict": {"type": "string", "enum": ["supported", "partially_supported", "not_supported"]},
                    "reason": STR,
                }
            ),
        }
    }
)

CODE_SUPPLEMENT_SCHEMA = _obj(
    {
        "items": {
            "type": "array",
            "items": _obj(
                {
                    "file": STR,
                    "hunk_new_start": INT,
                    "symbol": STR,
                    "kind": {"type": "string", "enum": ["prompt", "schema", "logic", "validation", "formatting", "other"]},
                    "summary": STR,
                }
            ),
        }
    }
)


# ---------------------------------------------------------------- pydantic 검증 모델
class SupplementItemM(BaseModel):
    change_id: str
    category: str
    user_intent: str
    agent_gap: str
    confidence: float = Field(ge=0.0, le=1.0)


class SupplementM(BaseModel):
    items: list[SupplementItemM]


class CodeLocationM(BaseModel):
    file: str
    line_start: int
    line_end: int
    quote: str


class EditM(BaseModel):
    file: str
    original_snippet: str
    replacement: str
    explanation: str


class ImprovementItemM(BaseModel):
    title: str
    problem: str
    recommendation: str
    category: str
    priority: Literal["high", "medium", "low"]
    evidence_change_ids: list[str]
    based_on: list[str]
    code_locations: list[CodeLocationM]
    proposed_edits: list[EditM]
    expected_effect: str


class ImprovementM(BaseModel):
    recommendations: list[ImprovementItemM]


class CrossCheckItemM(BaseModel):
    rec_id: str
    verdict: Literal["supported", "partially_supported", "not_supported"]
    reason: str


class CrossCheckM(BaseModel):
    results: list[CrossCheckItemM]


class CodeSupplementItemM(BaseModel):
    file: str
    hunk_new_start: int
    symbol: str
    kind: Literal["prompt", "schema", "logic", "validation", "formatting", "other"]
    summary: str


class CodeSupplementM(BaseModel):
    items: list[CodeSupplementItemM]
