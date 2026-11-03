"""실행 설정(YAML) 로더."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator


class CodeCompareSpec(BaseModel):
    base: str
    head: str
    label: Optional[str] = None


class TargetAgent(BaseModel):
    """분석 대상인 제품개발 프로세스 에이전트."""

    id: str
    name: str = ""
    stage: str = ""
    description: str = ""
    output_type: str = "auto"  # auto | xlsx | pptx | docx | csv | text | pdf | (플러그인 형식)
    repo: Optional[str] = None  # git URL 또는 로컬 경로
    ref: Optional[str] = None  # 브랜치/태그/커밋, 로컬 미커밋 상태는 WORKTREE
    paths: list[str] = Field(default_factory=list)  # 저장소 내 에이전트 코드 경로
    code_compare: list[CodeCompareSpec] = Field(default_factory=list)  # 사용자 보완 브랜치 비교
    header_rows: dict[str, int] = Field(default_factory=dict)  # 시트명 → 헤더 행 ("*" = 전체)
    key_columns: dict[str, str] = Field(default_factory=dict)  # 시트명 → 키 열 헤더명

    @field_validator("paths", mode="before")
    @classmethod
    def _paths(cls, v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else v


class PairSpec(BaseModel):
    """비교쌍: 에이전트 원본 산출물 vs 사용자 수정본(또는 다른 코드 버전의 산출물)."""

    agent: str
    baseline: str
    revised: str
    id: Optional[str] = None
    revised_by: Optional[str] = None
    baseline_ref: Optional[str] = None  # 원본 산출물을 생성한 코드 ref
    revised_ref: Optional[str] = None  # 수정본도 에이전트 생성물이면 그 코드 ref


class LLMOptions(BaseModel):
    """Azure 접속 정보(엔드포인트·키·배포·버전)는 .env 에서 읽는다. 여기서는 동작 옵션만 지정."""

    enabled: Union[bool, Literal["auto"]] = "auto"  # auto: .env 설정이 완전하면 사용
    deployment_main: Optional[str] = None  # .env 의 AZURE_OPENAI_DEPLOYMENT 대신 쓸 때만
    deployment_fast: Optional[str] = None
    reasoning_effort: Optional[str] = None
    api_version: Optional[str] = None
    timeout_seconds: Optional[float] = None  # 호출 1회 제한 (기본: .env AZURE_OPENAI_TIMEOUT 또는 120)
    budget_seconds: Optional[float] = None  # 실행 전체 LLM 시간 예산 (기본 1800, 0=무제한)
    supplement_batch_size: int = 40
    max_changes_per_agent: int = 300
    accept_reclassification_confidence: float = 0.7
    cross_check: bool = True
    max_context_chars: int = 60000
    use_tools: bool = True  # GPT 함수 호출로 코드·문서·변경 추가 조회
    max_tool_steps: int = 6
    max_tool_calls: int = 20
    tools: Optional[list[str]] = None  # 사용할 도구 이름 제한 (None = 등록된 전체)


class DiffOptions(BaseModel):
    compare_styles: bool = True
    compare_geometry: bool = True
    timeout_seconds: float = 300.0  # 비교쌍 1개 제한 시간 (0 = 제한 없음)
    isolation: Literal["process", "inline"] = "process"  # process: 별도 프로세스에서 비교 → 시간 초과 시 강제 종료


FailCategory = Literal["comparison", "integrity", "llm", "code", "report", "config"]


class StrictOptions(BaseModel):
    """strict 모드: 지정한 범주의 실패가 하나라도 있으면 실패 종료코드로 끝낸다 (보고서는 항상 작성)."""

    enabled: bool = False
    fail_on: list[FailCategory] = Field(default_factory=lambda: ["comparison", "integrity", "llm", "code", "report", "config"])
    fail_fast: bool = False  # 첫 실패에서 분석 중단 (부분 보고서 작성)


class RunConfig(BaseModel):
    project: str = "제품개발 에이전트 산출물 분석"
    output_dir: str = "rca_output"
    cache_dir: str = ".rca_cache"
    report_formats: list[str] = Field(default_factory=lambda: ["md", "xlsx", "json", "html"])
    env_file: Optional[str] = None  # Azure 설정 .env 경로 (기본: 설정 파일 폴더 → 현재 폴더의 .env)
    plugins: list[str] = Field(default_factory=list)  # 사용자 문서 핸들러/GPT 도구 모듈 또는 .py 경로
    llm: LLMOptions = Field(default_factory=LLMOptions)
    diff: DiffOptions = Field(default_factory=DiffOptions)
    strict: StrictOptions = Field(default_factory=StrictOptions)
    agents: list[TargetAgent] = Field(default_factory=list)
    comparisons: list[PairSpec] = Field(default_factory=list)
    base_dir: str = "."

    @classmethod
    def load(cls, path: str | Path) -> "RunConfig":
        path = Path(path)
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = cls.model_validate(data)
        cfg.base_dir = str(path.parent.resolve())
        cfg._validate_refs()
        return cfg

    def _validate_refs(self) -> None:
        ids = [a.id for a in self.agents]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"중복된 에이전트 id: {sorted(dup)}")
        for p in self.comparisons:
            if p.agent not in ids:
                raise ValueError(f"comparisons 의 agent '{p.agent}' 가 agents 에 정의되지 않았습니다")

    def resolve(self, p: Optional[str]) -> Optional[str]:
        """설정 파일 기준 상대경로를 절대경로로 (git URL 은 그대로)."""
        if p is None:
            return None
        if "://" in p or p.startswith("git@"):
            return p
        pp = Path(p).expanduser()
        if not pp.is_absolute():
            pp = Path(self.base_dir) / pp
        return str(pp.resolve())

    def pairs_for(self, agent_id: str) -> list[PairSpec]:
        out = []
        n = 0
        for p in self.comparisons:
            if p.agent == agent_id:
                n += 1
                out.append(p.model_copy(update={"id": p.id or f"{agent_id}-{n}"}))
        return out
