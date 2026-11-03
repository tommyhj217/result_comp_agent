# result_comp_agent

**제품개발 프로세스 에이전트 산출물(PPT · Excel) 비교 → 사용자 보완 분석 → 에이전트 코드 개선 권고** 를 수행하는 분석 에이전트입니다.
Azure OpenAI 의 **GPT-6 시리즈**(gpt-6 / gpt-6-mini 배포)를 해석·개선안 작성에 사용하며, **신뢰도**를 최우선으로 설계했습니다.

이 에이전트는 세 가지 질문에 근거와 함께 답합니다.

1. **산출물에서 무엇이 달라졌는가?** — 에이전트가 만든 원본과 사용자 최종본의 셀·행·열·시트 / 슬라이드·도형·문단·표·노트 단위 차이
2. **사용자는 무엇을 보완했는가?** — 보완 유형(내용 추가, 값 정정, 구체화, 구조 변경 …), 여러 사용자에게 반복되는 패턴, 사용자가 코드 브랜치에 직접 넣은 보완
3. **에이전트 코드에서 무엇을 고쳐야 하는가?** — git 의 에이전트 코드에서 해당 산출물을 만든 위치(파일:줄, 함수, 프롬프트, 컬럼/섹션 정의)와 적용 가능한 수정 패치

---

## 신뢰도 설계

| 원칙 | 구현 |
|---|---|
| **차이 검출은 LLM 을 쓰지 않는다** | 결정론적 diff 엔진이 모든 변경을 검출합니다. LLM 은 이미 검출된 변경을 *해석*만 하며 변경을 추가·삭제할 수 없습니다. |
| **diff 무결성 검증** | 비교쌍마다 `원본 + 보고된 변경 목록 = 수정본` 을 실제로 재구성해 대조합니다. 누락·오보고가 있으면 `FAIL` 과 위치를 보고합니다 (`--strict` 시 종료코드 2). 또한 각 셀 수정의 "이전 값"이 원본과 같은지 검사합니다. |
| **서식 변경 분리** | `1,000` vs `1000`, 공백, `2025.01.05` vs 날짜, 셀 서식, 도형 위치 변경은 `format_only` 로 분리해 내용 변경과 섞이지 않게 합니다 (숨기지 않고 별도 집계). |
| **대응(pairing) 근거 기록** | 행은 ID 키 열 → 유사도 DP 순으로 대응하고, 각 변경에 `pairing`(key/header/exact/similarity/moved)과 점수를 남깁니다. 유사도 0.7 미만 대응은 '낮은 신뢰도 대응'으로 집계됩니다. |
| **코드 추적은 결정론적 매칭** | 컬럼명·시트명·슬라이드 제목·하드코딩 값을 코드 리터럴/프롬프트/템플릿에서 찾고 방식(정확·정규화·부분·유사·형제 컨테이너 추론)과 점수를 기록합니다. 못 찾으면 **'추적 불가'로 정직하게 표시**합니다. |
| **LLM 출력 근거 검증** | GPT 응답의 `change_id` 실재, 인용 코드(quote)가 해당 파일·줄에 실제 존재하는지 확인합니다. 다른 줄에 있으면 줄 번호를 보정하고, 없으면 제거합니다. 근거가 없는 권고는 **기각(REJECTED)** 되어 본문에서 빠지고 부록에만 남습니다. |
| **수정 패치 검증** | 제안된 수정의 원본 스니펫이 파일에 **정확히 1회** 존재해야 하며, Python 은 적용 후 구문 검사를 통과해야 unified diff 패치를 생성합니다. |
| **자기 교정 루프** | 스키마 위반은 오류를 알려 재요청, 근거 문제(없는 ID·코드에 없는 인용)는 문제 목록을 피드백해 재작성 요청합니다. 끝까지 남은 문제 항목만 검증 단계에서 개별 기각합니다. |
| **교차 검증** | gpt-6-mini 가 "증거가 권고를 뒷받침하는가"를 판정합니다. `not_supported` 인 LLM 권고는 기각됩니다. |
| **규칙 기준선 + LLM 재분류 제한** | 규칙 기반 분류는 항상 수행됩니다. LLM 은 내용 계열 유형끼리, 신뢰도 ≥ 0.7 일 때만 재분류할 수 있고(서식/구조/메모 같은 사실 분류는 변경 불가) 규칙-LLM 일치율이 보고됩니다. |
| **재현성** | Structured Outputs(strict JSON schema), seed 고정, 동일 입력 응답 디스크 캐시 → 같은 입력이면 같은 결과. |
| **투명한 신뢰도 점수** | `0.4·증거강도 + 0.3·코드근거 + 0.1·수정안유효성 + 0.2·일관성` — 구성 요소를 모두 보고서에 표시합니다. |

---

## 구성

```
                    ┌──────────────── Orchestrator ────────────────┐
 원본 PPT/Excel ──▶ │ DiffAgent (결정론)                           │
 수정본 PPT/Excel ─▶ │   추출 → 정렬(키/DP) → 변경 목록 → 무결성 검증 │
                    │ CodeAgent (결정론)                           │◀── git 저장소 (에이전트 코드)
                    │   코드 인덱스(AST 리터럴/프롬프트/템플릿)       │     · 기준 ref
                    │   변경→코드 추적 · 사용자 브랜치 diff           │     · 사용자 보완 브랜치
                    │ SupplementAnalystAgent (규칙 + GPT-6)        │
                    │   보완 유형 · 사용자 의도 · 에이전트 갭         │
                    │ Aggregator (결정론)  반복 패턴 · 공통 패턴      │
                    │ ImprovementAdvisorAgent (규칙 + GPT-6)       │
                    │   코드 위치 · 수정안                           │
                    │ VerifierAgent (결정론 + GPT-6-mini 교차검증)   │
                    │   근거/인용/패치 검증 · 신뢰도                  │
                    └──────────────▶ Markdown · Excel · HTML · JSON 보고서
```

```
src/result_comp_agent/
  extractors/   excel.py, pptx.py            산출물 스냅샷 추출
  diff/         normalize, align, grid,      결정론적 diff + 무결성 검증
                excel_diff, pptx_diff, compare
  code/         git_source, indexer,         git 코드 로드 · 인덱싱 · 추적 · 코드 diff
                tracer, code_diff
  analysis/     classifier, aggregator,      보완 유형 분류 · 패턴 집계 · 규칙 권고
                recommender
  llm/          client, schemas, prompts     Azure OpenAI GPT-6 (structured outputs, 캐시, 재시도)
  agents/       orchestrator, code_agent,    하위 에이전트
                supplement_agent, improvement_agent,
                code_supplement_agent, verifier_agent, grounding
  report/       markdown, excel, html        보고서
examples/
  pd_agents/    샘플 제품개발 에이전트(시장분석 PPT · 요구사항 Excel · 테스트케이스 Excel)
  build_demo.py 데모 작업공간(git 저장소 + 산출물 + 사용자 수정본 + 설정) 생성
```

---

## 빠른 시작

```bash
pip install -e .            # Python 3.10+
# (선택) Entra ID 인증: pip install -e ".[entra]"

# 1) 데모: 샘플 에이전트 git 저장소 · 산출물 · 사용자 수정본 생성
python examples/build_demo.py demo_workspace

# 2) 분석 (Azure 설정이 없으면 규칙 기반으로 실행)
rca run -c demo_workspace/config.yaml
#  → demo_workspace/rca_output/report.md | report.xlsx | report.html | analysis.json
```

데모에서 확인할 수 있는 결과 예:

- 두 사용자가 모두 요구사항 정의서에 `리스크` 열을 추가 → `requirements_agent.py:17-18 (COLUMNS)` **'우선순위'와 '담당자' 사이**에 추가하라는 권고, 사용자 브랜치 `feature/kim-risk-column` 에 같은 보완이 이미 있다는 근거 첨부
- 사용자가 `기획팀 → 모바일팀` 수정 → 하드코딩 상수 `DEFAULT_OWNER = "기획팀"` (`requirements_agent.py:21`) 추적
- 두 사용자가 모두 `가격 전략` 슬라이드 추가 → `SECTIONS` 의 '타겟 고객'과 '결론 및 제언' 사이 삽입 위치
- 응답시간 `2초 → 1초 (p95)` 정정 → 상세설명 작성 지침이 있는 `SYSTEM_PROMPT` 줄

### 단일 비교 / 코드 비교

```bash
# 원본 vs 수정본 하나만 (코드 추적 선택)
rca diff agent_output.xlsx user_final.xlsx -o out --repo https://github.com/org/pd-agents.git --ref main --paths agents/requirements

# 에이전트 코드 ref 간 비교 (사용자 보완 브랜치 등)
rca code-diff --repo ./pd-agents --base main --head feature/kim-risk-column --paths agents/requirements
```

---

## Azure OpenAI (GPT-6) 설정

`.env.example` 참고. 환경 변수만 설정하면 자동으로 GPT-6 경로가 활성화됩니다 (`llm.enabled: auto`).

| 변수 | 설명 | 기본값 |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` | — |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_USE_ENTRA_ID` | API 키 또는 Entra ID(DefaultAzureCredential) | — |
| `AZURE_OPENAI_API_VERSION` | api-version, `v1` 이면 v1 엔드포인트 사용 | `2025-04-01-preview` |
| `AZURE_OPENAI_DEPLOYMENT_MAIN` | 해석·개선안 배포 이름 | `gpt-6` |
| `AZURE_OPENAI_DEPLOYMENT_FAST` | 교차검증 배포 이름 | `gpt-6-mini` |
| `AZURE_OPENAI_REASONING_EFFORT` | 추론 강도 (빈 값이면 미전송) | `medium` |
| `AZURE_OPENAI_TEMPERATURE` | 추론 모델은 미지원일 수 있어 기본 미전송 | — |
| `AZURE_OPENAI_SEED` | 재현성 | `42` |

> GPT-6 시리즈의 배포 이름·api-version·지원 파라미터는 조직의 Azure 리소스에 맞게 위 변수로 조정하세요.
> 호출은 Chat Completions + `response_format: json_schema (strict)` 를 사용합니다. 오프라인 모드는 `--offline` 또는 `llm.enabled: false`.

LLM 호출 지점 (모두 근거 검증 대상):

| 단계 | 배포 | 출력 | 검증 |
|---|---|---|---|
| 보완 해석 | gpt-6 | 변경별 유형·사용자 의도·에이전트 갭 | change_id 실재/누락, 재분류 제한 |
| 코드 개선안 | gpt-6 | 권고·코드 위치(quote)·수정안 | ID 실재, quote 위치, 패치 1회 일치·구문 |
| 사용자 코드 보완 요약 | gpt-6 | hunk 별 보완 요약 | file+hunk 실재 |
| 교차 검증 | gpt-6-mini | supported / partial / not_supported | rec_id 실재 |

---

## 설정 파일

`config/agents.example.yaml` 에 전체 예시가 있습니다. 핵심 항목:

```yaml
agents:
  - id: requirements
    name: 요구사항 정의 에이전트
    stage: "2. 요구사항 정의"        # 보고서 정렬 기준 (제품개발 프로세스 단계)
    output_type: xlsx               # xlsx | pptx | auto
    repo: https://github.com/org/pd-agents.git   # 또는 로컬 경로
    ref: main                       # 로컬 미커밋 작업 트리: WORKTREE
    paths: [agents/requirements, common]          # 코드·프롬프트(.md/.txt/.yaml)·템플릿(.xlsx/.pptx) 경로
    key_columns: {"요구사항정의서": "요구사항ID"}  # (선택) 행 대응 키 강제
    code_compare: [{base: main, head: feature/kim, label: 김OO 보완}]
comparisons:
  - {agent: requirements, baseline: outputs/req_agent.xlsx, revised: final/req_kim.xlsx, revised_by: 김OO}
```

사설 저장소는 git credential helper 를 쓰거나 `RCA_GIT_AUTH_HEADER` 를 지정하세요. 원격 저장소는 `cache_dir/repos` 에 bare clone 후 `git show` 로 읽으며 체크아웃하지 않습니다.

---

## 보고서

| 파일 | 내용 |
|---|---|
| `report.md` | 요약 · 신뢰도 검증 · 에이전트별 차이/패턴/권고(코드 스니펫·패치) · 공통 패턴 · 기각 권고 부록 |
| `report.html` | 단일 파일 대시보드 (검색 가능한 변경 목록, 라이트/다크) |
| `report.xlsx` | `요약` `변경사항`(전체, 필터) `보완패턴` `개선권고` `코드추적` `코드보완` `무결성검증` 시트 |
| `analysis.json` | 전체 결과 (alignment, 근거, 검증 내역 포함) — 다른 시스템 연동용 |

### 검출 변경 유형

| Excel | PPT |
|---|---|
| 시트 추가/삭제/이름 변경 | 슬라이드 추가/삭제/순서 변경, 레이아웃 변경 |
| 행 추가/삭제/순서 변경, 열 추가/삭제/순서 변경 | 도형 추가/삭제, 위치·크기 변경 |
| 셀 값 수정(헤더=컬럼명 변경 포함), 수식 변경(참조 이동 표시) | 문단 추가/삭제/수정(단어 단위 diff, 수치 변화) |
| 메모 추가/수정/삭제, 서식 변경(열 단위 집계) | 표 셀/행/열 변경 (엑셀과 같은 엔진), 차트·이미지 변경, 발표자 노트 |

### 보완 유형

내용 추가 · 내용 삭제 · 값·사실 정정 · 내용 보강·구체화 · 내용 재작성 · 표현 수정 · 구조 변경 · 검토 의견·메모 · 수식 변경 · 시각 자료 변경 · 서식·표기 변경

---

## 한계 (정확한 해석을 위해)

- **무결성 검증의 범위**: 추출한 내용(셀 값·수식 문자열·문단 텍스트·표·노트·이미지 해시·차트 데이터) 기준으로 변경 목록이 완전하고 정확함을 보장합니다. 어떤 행이 어떤 행과 대응되는지의 *의미적* 판단은 대응 방식·점수로 별도 표시됩니다.
- **비교하지 않는 항목**: 엑셀 병합 셀·데이터 유효성·조건부 서식 규칙·엑셀 차트, PPT 애니메이션·SmartArt 내부·글꼴 세부 서식.
- **코드 추적**: 문자열 리터럴·프롬프트·템플릿 기반입니다. 런타임에 외부(DB·API)에서 오는 이름은 추적할 수 없으며 '추적 불가'로 표시됩니다. LLM 이 생성한 본문 내용은 코드에 없으므로, 내용 품질 문제는 같은 모듈의 생성 프롬프트를 수정 후보로 연결합니다(`same_module`, 낮은 점수로 표시).
- **패치**: 인덱스는 줄바꿈을 LF 로 정규화하므로 CRLF 파일에는 `git apply --ignore-whitespace` 가 필요할 수 있습니다.

---

## 개발

```bash
pip install -e ".[dev]"
pytest -q          # 결정론 diff/무결성, 코드 추적·git, 검증기, 가짜 Azure 전송계층으로 GPT-6 경로(환각 응답 포함) 테스트
```
