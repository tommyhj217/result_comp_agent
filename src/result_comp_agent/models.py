"""분석 전 과정에서 공유하는 데이터 모델.

설계 원칙
---------
* ``Change`` 는 결정론적 diff 엔진만 생성한다. LLM 은 Change 를 만들거나 지우지 못하고,
  이미 존재하는 Change 에 해석(category/user_intent/agent_gap)만 덧붙일 수 있다.
* 모든 권고(``Recommendation``)는 근거 Change ID 와 코드 위치(``CodeRef``)를 가지며,
  ``VerificationResult`` 로 검증 상태가 기록된다.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ArtifactType(str, Enum):
    """기본 제공 문서 형식. 플러그인 핸들러는 임의의 문자열 형식을 쓸 수 있다(모델 필드는 str)."""

    XLSX = "xlsx"
    PPTX = "pptx"
    DOCX = "docx"
    CSV = "csv"
    TEXT = "text"
    PDF = "pdf"


def art(v) -> str:
    """ArtifactType 또는 문자열 → 문자열."""
    return getattr(v, "value", v)


class ChangeType(str, Enum):
    # 문서(통합문서/프레젠테이션) 수준
    SHEET_ADDED = "sheet_added"
    SHEET_REMOVED = "sheet_removed"
    SHEET_RENAMED = "sheet_renamed"
    SLIDE_ADDED = "slide_added"
    SLIDE_REMOVED = "slide_removed"
    SLIDE_MOVED = "slide_moved"
    LAYOUT_CHANGED = "layout_changed"
    # 표(시트 / PPT 표) 수준
    ROW_ADDED = "row_added"
    ROW_REMOVED = "row_removed"
    ROW_MOVED = "row_moved"
    COLUMN_ADDED = "column_added"
    COLUMN_REMOVED = "column_removed"
    COLUMN_MOVED = "column_moved"
    CELL_MODIFIED = "cell_modified"
    COMMENT_ADDED = "comment_added"
    COMMENT_REMOVED = "comment_removed"
    COMMENT_MODIFIED = "comment_modified"
    STYLE_MODIFIED = "style_modified"
    # 도형/텍스트 수준 (PPT)
    SHAPE_ADDED = "shape_added"
    SHAPE_REMOVED = "shape_removed"
    SHAPE_GEOMETRY_CHANGED = "shape_geometry_changed"
    TEXT_ADDED = "text_added"
    TEXT_REMOVED = "text_removed"
    TEXT_MODIFIED = "text_modified"
    IMAGE_REPLACED = "image_replaced"
    CHART_MODIFIED = "chart_modified"
    # 문서(Word/Markdown/PDF) 및 시트 속성
    TABLE_ADDED = "table_added"
    TABLE_REMOVED = "table_removed"
    SHEET_PROPERTY_CHANGED = "sheet_property_changed"


CHANGE_TYPE_LABELS: dict[str, str] = {
    "sheet_added": "시트 추가",
    "sheet_removed": "시트 삭제",
    "sheet_renamed": "시트 이름 변경",
    "slide_added": "슬라이드 추가",
    "slide_removed": "슬라이드 삭제",
    "slide_moved": "슬라이드 순서 변경",
    "layout_changed": "레이아웃 변경",
    "row_added": "행 추가",
    "row_removed": "행 삭제",
    "row_moved": "행 순서 변경",
    "column_added": "열 추가",
    "column_removed": "열 삭제",
    "column_moved": "열 순서 변경",
    "cell_modified": "셀 값 수정",
    "comment_added": "메모 추가",
    "comment_removed": "메모 삭제",
    "comment_modified": "메모 수정",
    "style_modified": "서식 변경",
    "shape_added": "도형 추가",
    "shape_removed": "도형 삭제",
    "shape_geometry_changed": "도형 위치/크기 변경",
    "text_added": "문단 추가",
    "text_removed": "문단 삭제",
    "text_modified": "문단 수정",
    "image_replaced": "이미지 교체",
    "chart_modified": "차트 변경",
    "table_added": "표 추가",
    "table_removed": "표 삭제",
    "sheet_property_changed": "시트 속성 변경",
}


class Location(BaseModel):
    """변경 위치. ``path`` 는 사람이 읽는 경로, 나머지는 구조화된 좌표."""

    artifact: str
    container: str  # 무결성 검증 단위 ID (예: "sheet:요구사항", "slide[3]/shape[5]/text")
    path: str
    part: str = ""  # sheet | cell | header | row | column | slide | shape | text | notes | table
    sheet: Optional[str] = None
    cell: Optional[str] = None  # 수정본 기준 A1 주소 (삭제는 원본 기준)
    base_cell: Optional[str] = None
    row_label: Optional[str] = None
    column_label: Optional[str] = None
    slide_index: Optional[int] = None  # 수정본 기준 (삭제는 원본 기준)
    base_slide_index: Optional[int] = None
    slide_title: Optional[str] = None
    shape_name: Optional[str] = None
    section: Optional[str] = None  # 문서형(Word/Markdown/PDF)의 소속 제목(섹션)
    page: Optional[int] = None

    @field_validator("artifact", mode="before")
    @classmethod
    def _art(cls, v):
        return art(v)


class Anchor(BaseModel):
    """코드 추적에 사용할 검색어와 그 역할."""

    text: str
    role: str  # sheet | column | new_column | section | new_section | value | new_value | neighbor_left | neighbor_right


class CodeRef(BaseModel):
    agent_id: str
    commit: str
    file: str
    line_start: int
    line_end: int
    symbol: Optional[str] = None
    kind: str  # literal | prompt | fstring | template | text_line | docstring | insertion_point | code_change
    match_method: str  # exact | normalized | substring | fuzzy | neighbor | container | diff
    anchor: str
    anchor_role: str
    matched_text: str
    score: float
    snippet: str = ""
    sub_location: Optional[str] = None  # 템플릿 파일 내부 위치 등
    note: Optional[str] = None


class Change(BaseModel):
    id: str
    pair_id: str
    agent_id: str
    artifact: str
    change_type: ChangeType
    location: Location
    before: Optional[str] = None
    after: Optional[str] = None
    format_only: bool = False
    pairing: str = "n/a"  # key | header | exact | similarity | id | moved | n/a
    pairing_score: float = 1.0
    detail: dict[str, Any] = Field(default_factory=dict)
    anchors: list[Anchor] = Field(default_factory=list)
    # ---- 분석 단계에서 채워짐 ----
    category: Optional[str] = None
    category_rule: Optional[str] = None
    llm_category: Optional[str] = None
    category_agreement: Optional[bool] = None
    user_intent: Optional[str] = None
    agent_gap: Optional[str] = None
    code_refs: list[CodeRef] = Field(default_factory=list)
    code_change_refs: list[CodeRef] = Field(default_factory=list)

    @field_validator("artifact", mode="before")
    @classmethod
    def _art(cls, v):
        return art(v)

    @property
    def type_label(self) -> str:
        return CHANGE_TYPE_LABELS.get(self.change_type.value, self.change_type.value)


class IntegrityReport(BaseModel):
    """diff 무결성 검증 결과: 원본 + 변경목록 → 수정본 재구성 일치 여부."""

    status: Literal["PASS", "FAIL"]
    checks: int = 0
    failures: list[str] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    pair_id: str
    agent_id: str
    artifact: str
    baseline_path: str
    revised_path: str
    baseline_sha256: str
    revised_sha256: str
    revised_by: Optional[str] = None
    baseline_ref: Optional[str] = None
    revised_ref: Optional[str] = None
    changes: list[Change] = Field(default_factory=list)
    alignment: dict[str, Any] = Field(default_factory=dict)
    integrity: IntegrityReport
    stats: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    seconds: Optional[float] = None

    @field_validator("artifact", mode="before")
    @classmethod
    def _art(cls, v):
        return art(v)


class StepFailure(BaseModel):
    """실행 단계 실패 — 보고서에 '실패'로 명시하고 strict 판정에 사용."""

    category: Literal["comparison", "integrity", "llm", "code", "report", "config"]
    stage: str  # diff | code.load | code.branch | llm.supplement | llm.improvement | llm.crosscheck | llm.code_supplement | report.html ...
    kind: str  # timeout | file_not_found | unsupported | parse_error | auth | not_found | rate_limit | validation | ...
    target: str  # 비교쌍 ID / 에이전트 ID / 파일
    message: str
    impact: str = ""  # 결과에 미친 영향 (예: '규칙 기반 결과만 표시')


class FailedComparison(BaseModel):
    pair_id: str
    agent_id: str
    baseline_path: str
    revised_path: str
    revised_by: Optional[str] = None
    kind: str
    message: str
    seconds: Optional[float] = None


class Pattern(BaseModel):
    """여러 비교쌍(사용자)에서 반복되는 보완 패턴."""

    id: str
    agent_id: str
    key: str
    title: str
    change_type: str
    category: str
    location_hint: str
    support_pairs: list[str]
    support_ratio: float
    change_ids: list[str]
    examples: list[dict[str, Any]] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    user_code_evidence: list[CodeRef] = Field(default_factory=list)


class ProposedEdit(BaseModel):
    file: str
    original_snippet: str
    replacement: str
    explanation: str = ""
    verified: bool = False
    verification_note: str = ""
    patch: Optional[str] = None


class VerificationResult(BaseModel):
    status: Literal["VERIFIED", "PARTIAL", "UNVERIFIED", "REJECTED"] = "UNVERIFIED"
    checks: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    llm_cross_check: Optional[str] = None  # supported | partially_supported | not_supported
    llm_cross_check_reason: Optional[str] = None


class Recommendation(BaseModel):
    id: str
    agent_id: str
    title: str
    problem: str
    recommendation: str
    category: str
    priority: Literal["high", "medium", "low"] = "medium"
    source: Literal["rule", "llm"] = "rule"
    evidence_change_ids: list[str] = Field(default_factory=list)
    pattern_ids: list[str] = Field(default_factory=list)
    code_refs: list[CodeRef] = Field(default_factory=list)
    proposed_edits: list[ProposedEdit] = Field(default_factory=list)
    expected_effect: str = ""
    based_on: list[str] = Field(default_factory=list)
    verification: VerificationResult = Field(default_factory=VerificationResult)
    confidence: float = 0.0
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)


class DiffHunk(BaseModel):
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    symbol: Optional[str] = None
    added: list[tuple[int, str]] = Field(default_factory=list)
    removed: list[tuple[int, str]] = Field(default_factory=list)


class FileDiffReport(BaseModel):
    path: str
    old_path: Optional[str] = None
    status: str  # added | deleted | modified | renamed | binary
    added_lines: int = 0
    removed_lines: int = 0
    hunks: list[DiffHunk] = Field(default_factory=list)


class CodeSupplementItem(BaseModel):
    file: str
    symbol: Optional[str] = None
    kind: str  # prompt | schema | logic | validation | formatting | other
    summary: str
    hunk_new_start: Optional[int] = None
    verified: bool = False


class CodeDiffReport(BaseModel):
    agent_id: str
    label: str
    base_ref: str
    base_commit: str
    head_ref: str
    head_commit: str
    files: list[FileDiffReport] = Field(default_factory=list)
    literals_added: list[str] = Field(default_factory=list)
    literals_removed: list[str] = Field(default_factory=list)
    supplements: list[CodeSupplementItem] = Field(default_factory=list)
    error: Optional[str] = None


class AgentAnalysis(BaseModel):
    agent_id: str
    name: str
    stage: str
    output_type: str
    repo: Optional[str] = None
    ref: Optional[str] = None
    commit: Optional[str] = None
    code_files_indexed: int = 0
    code_error: Optional[str] = None
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    patterns: list[Pattern] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    rejected_recommendations: list[Recommendation] = Field(default_factory=list)
    code_diffs: list[CodeDiffReport] = Field(default_factory=list)
    trace_stats: dict[str, Any] = Field(default_factory=dict)
    llm_notes: list[str] = Field(default_factory=list)
    failures: list[StepFailure] = Field(default_factory=list)
    failed_comparisons: list[FailedComparison] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    project: str
    generated_at: str
    tool_version: str
    llm: dict[str, Any] = Field(default_factory=dict)
    agents: list[AgentAnalysis] = Field(default_factory=list)
    cross_agent_patterns: list[dict[str, Any]] = Field(default_factory=list)
    reliability: dict[str, Any] = Field(default_factory=dict)
    status: Literal["SUCCESS", "PARTIAL", "FAILED"] = "SUCCESS"
    failures: list[StepFailure] = Field(default_factory=list)
    strict: dict[str, Any] = Field(default_factory=dict)  # {enabled, fail_on, violated, exit_code, aborted}
    exit_code: int = 0
    plugins: list[str] = Field(default_factory=list)
