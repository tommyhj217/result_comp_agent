# result_comp_agent

**제품개발 프로세스 에이전트 산출물(Excel · PPT · Word · CSV · Markdown · PDF) 비교 → 사용자 보완 분석 → 에이전트 코드 개선 권고** 를 수행하는 분석 에이전트입니다.
Azure OpenAI 의 **GPT 배포**(`.env` 의 4개 키)를 해석·개선안 작성에 사용하며, **신뢰도**를 최우선으로 설계했습니다.

이 에이전트는 세 가지 질문에 근거와 함께 답합니다.

1. **산출물에서 무엇이 달라졌는가?** — 에이전트 원본과 사용자 최종본의 셀·행·열·시트 / 슬라이드·도형·문단·표·노트 / 섹션·문단·표·메모 단위 차이
2. **사용자는 무엇을 보완했는가?** — 보완 유형(내용 추가, 값 정정, 구체화, 구조 변경 …), 여러 사용자에게 반복되는 패턴, 사용자가 코드 브랜치에 직접 넣은 보완
3. **에이전트 코드에서 무엇을 고쳐야 하는가?** — git 의 에이전트 코드에서 해당 산출물을 만든 위치(파일:줄, 함수, 프롬프트, 컬럼/섹션 정의)와 적용 가능한 수정 패치

---

## 빠른 시작

```bash
pip install -e .                 # Python 3.10+   (PDF 비교: pip install -e ".[pdf]")
cp .env.example .env             # Azure OpenAI 4개 키 입력 (없으면 규칙 기반으로 동작)
rca check-env --probe            # .env 점검 + 실제 GPT 배포 호출 확인

python examples/build_demo.py demo_workspace     # 샘플 에이전트 git 저장소·산출물·사용자 수정본·설정 생성
rca run -c demo_workspace/config.yaml            # → demo_workspace/rca_output/report.{md,xlsx,html} · analysis.json
```

---

## Azure OpenAI(GPT) 설정 — `.env`

```dotenv
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_DEPLOYMENT=<GPT 배포 이름>      # Azure 포털 > 배포 '이름' (모델명 아님)
AZURE_OPENAI_API_VERSION=2024-10-21           # 2025-04-01-preview 등, v1 엔드포인트면 v1
```

- **탐색 순서**: `--env-file` → `RCA_ENV_FILE` → 설정 파일 폴더의 `.env` → 현재 폴더의 `.env`. 실제 환경변수가 `.env` 보다 우선(`--env-override` 로 반대).
- **오타·별칭 허용**: `AZUTE_OPEN_API_VERSION`, `AZURE_OPEN_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT_NAME` 등도 읽고, `rca check-env` 가 표준 이름 사용을 안내합니다.
- **전체 URL 붙여넣기 허용**: `…/openai/deployments/<배포>/chat/completions?api-version=…` 형태면 엔드포인트·배포·버전을 분해해 사용합니다.
- 선택 키: `AZURE_OPENAI_DEPLOYMENT_FAST`(교차검증용, 없으면 같은 배포), `AZURE_OPENAI_TIMEOUT`(호출당 초), `RCA_LLM_BUDGET_SECONDS`(전체 예산), `AZURE_OPENAI_REASONING_EFFORT`, `AZURE_OPENAI_TEMPERATURE`, `AZURE_OPENAI_SEED`, `AZURE_OPENAI_USE_ENTRA_ID` — `.env.example` 참고.

```text
$ rca check-env --probe
.env 파일: /work/.env
  AZURE_OPENAI_ENDPOINT     = https://my-res.openai.azure.com   [.env]
  AZURE_OPENAI_API_KEY      = abc1…tkey                          [.env]
  AZURE_OPENAI_DEPLOYMENT   = gpt-4o-prod                        [.env]
  AZURE_OPENAI_API_VERSION  = 2024-10-21                         [.env] (키 'AZUTE_OPEN_API_VERSION')
  ⚠ 'AZUTE_OPEN_API_VERSION' 키를 'AZURE_OPENAI_API_VERSION' 로 해석했습니다 — 표준 이름 사용을 권장합니다
배포 'gpt-4o-prod' 점검 중…
  ✅ 응답 성공 (1.2초) · 응답 모델: gpt-4o-2024-08-06
  응답 형식: json_schema · 토큰 파라미터: max_completion_tokens · 함수 호출: supported
```

### 배포된 GPT 모델에 맞춘 자동 적응

GPT 모델·api-version 마다 지원 파라미터가 다르므로, 400 응답의 `param` 을 보고 **배포별로 학습**합니다(`.rca_cache/llm/capabilities.json` 에 저장 → 다음 실행부터 실패 호출 없음).

| 상황 | 자동 처리 |
|---|---|
| `reasoning_effort` / `temperature` / `seed` 미지원 | 해당 파라미터 제외 |
| `max_completion_tokens` ↔ `max_tokens` | 모델이 요구하는 쪽으로 전환 |
| Structured Outputs(`json_schema`) 미지원 (구버전 api-version 등) | `json_object` → 프롬프트 지시로 단계적 하향, **로컬 스키마 검증은 항상 유지** |
| 함수 호출(`tools`) 미지원 | 도구 없이 사전 수집 컨텍스트로 진행 |
| 응답 모델 | 실제 응답의 `model`(예: `gpt-4o-2024-08-06`)을 보고서에 표시 |

---

## 실패 표시 · 시간 초과 · strict

모든 실패는 **범주·단계·종류·대상·내용·영향**으로 기록되어 보고서(HTML/MD/Excel `실패내역`/JSON)와 콘솔에 표시됩니다. 실행 결과는 `성공 / 부분 실패 / 실패` 로 요약됩니다.

| 범주 | 예 | 처리 |
|---|---|---|
| `comparison` 산출물 비교 | 파일 없음, 손상/암호 문서, 지원하지 않는 형식, 형식 불일치, **비교 시간 초과**, 비교 프로세스 비정상 종료 | 해당 비교쌍만 제외하고 계속 |
| `integrity` 무결성 | 원본+변경목록≠수정본 | 결과는 참고용으로 표시 |
| `llm` GPT | **시간 초과**, 연결, 인증, 배포 없음, 요청 한도, 콘텐츠 필터, 응답 검증 실패, 시간 예산 소진 | 규칙 기반 결과로 대체하고 '영향'에 명시 |
| `code` 에이전트 코드 | 브랜치/커밋 없음, 클론 실패, 코드 파일 없음 | 코드 추적 없이 진행 |
| `report` / `config` | 보고서 형식별 작성 실패, 플러그인 로드 실패, `.env` 불완전 | 다른 형식은 계속 작성 |

- **비교 시간 초과**: 비교쌍마다 별도 프로세스에서 실행하고 `diff.timeout_seconds`(기본 300초, `--timeout`)를 넘기면 프로세스를 종료하고 실패로 표시합니다.
- **GPT 시간 초과**: 호출당 `AZURE_OPENAI_TIMEOUT`(기본 120초, `--llm-timeout`), 일시적 오류(시간초과·연결·429·5xx)만 백오프 재시도(Retry-After 준수). 인증 오류·배포 없음은 **회로 차단**해 이후 호출을 즉시 실패시키고, 전체 예산(`RCA_LLM_BUDGET_SECONDS`, `--llm-budget`)을 넘기면 남은 GPT 단계는 실패로 표시됩니다.
- **strict**: `--strict`(또는 `strict.enabled`)이면 `fail_on` 범주의 실패가 있을 때 실패 종료코드를 반환합니다. `--strict-on comparison,integrity` 로 범주 지정, `--fail-fast` 는 첫 실패에서 분석을 중단합니다. 보고서는 항상 작성됩니다.

| 종료코드 | 의미 |
|---|---|
| 0 | 성공 (strict 미사용 시 부분 실패 포함) |
| 1 | 분석 실패 (모든 비교 실패 등) |
| 2 / 3 / 4 / 5 / 6 / 7 | strict 위반: 무결성 / 비교 / GPT / 코드 / 보고서 / 설정 |

---

## 신뢰도 설계

| 원칙 | 구현 |
|---|---|
| **차이 검출은 LLM 을 쓰지 않는다** | 결정론적 diff 엔진이 모든 변경을 검출하고, GPT 는 검출된 변경을 *해석*만 합니다. |
| **diff 무결성 검증** | 모든 형식에서 `원본 + 보고된 변경 목록 = 수정본` 을 실제로 재구성해 대조하고, 각 수정의 "이전 값"이 원본과 같은지 검사합니다. |
| **서식 변경 분리** | `1,000` vs `1000`, 공백, 날짜 표기, 셀 서식, 도형 위치, 조건부 서식 등은 `format_only` 로 분리합니다 (숨기지 않음). |
| **대응 근거 기록** | 행은 ID 키 열 → 유사도 DP, 각 변경에 대응 방식·점수를 남기고 낮은 신뢰도 대응을 집계합니다. |
| **코드 추적은 결정론적 매칭** | 컬럼명·시트명·섹션·하드코딩 값을 코드 리터럴/프롬프트/템플릿에서 찾고 방식·점수를 기록, 못 찾으면 '추적 불가'. |
| **GPT 출력 근거 검증** | `change_id` 실재, 인용 코드가 해당 파일·줄에 실제 존재하는지 확인(다른 줄이면 보정, 없으면 제거), 근거 없는 권고는 기각해 부록으로. |
| **수정 패치 검증** | 원본 스니펫이 파일에 **정확히 1회** 존재 + Python 구문 검사 통과 시에만 unified diff 패치 생성. |
| **자기 교정 루프** | 스키마 위반·근거 문제(없는 ID·코드에 없는 인용)를 GPT 에 피드백해 재작성, 끝까지 남은 항목만 개별 기각. |
| **교차 검증·재현성** | 교차검증 배포가 "증거가 권고를 뒷받침하는가"를 판정. Structured Outputs·seed·응답 캐시로 같은 입력 → 같은 결과. |
| **투명한 신뢰도 점수** | `0.4·증거강도 + 0.3·코드근거 + 0.1·수정안유효성 + 0.2·일관성`, 구성 요소를 모두 표시. |

---

## 지원 문서 형식 (`rca tools`, `rca inspect <파일>`)

| 형식 | 비교 범위 |
|---|---|
| Excel `.xlsx/.xlsm` | 시트·행·열·셀 값·수식(참조 이동 표시)·메모·서식 + **데이터 유효성(입력 규칙)·조건부 서식·병합 셀·틀 고정·자동 필터·시트 표시 상태** (범위는 열 헤더 이름 기준 비교 → 행 삽입에 흔들리지 않음) |
| PowerPoint `.pptx` | 슬라이드(추가/삭제/이동/레이아웃)·도형·문단·표·차트 데이터·이미지·**SmartArt 텍스트**·발표자 노트 |
| Word `.docx` | 제목(섹션)·문단·목록·표·메모, **변경 내용 추적이 남은 문서는 '모두 승인' 기준**으로 비교하고 그 사실을 표시 |
| CSV/TSV | 표 비교 (UTF-8/CP949 인코딩·구분자 자동 판별, 인코딩 차이 표시) |
| Markdown `.md` / 텍스트 `.txt` | 제목·문단·목록·코드·표 / 줄 단위 |
| PDF `.pdf` | 페이지별 텍스트 줄 (`pip install pypdf`, 스캔 PDF 는 OCR 필요 안내) |

---

## GPT 도구(함수 호출) — 조회 범위 확장

개선안 단계에서 GPT 는 미리 넣어준 컨텍스트 밖의 코드·문서도 **직접 조회**할 수 있습니다(모두 읽기 전용, 결과 크기 제한, 호출 기록을 보고서에 표시). 도구 조회 결과는 최종 구조화 응답에 텍스트로 전달되어 모델/버전에 무관하게 동작하며, 결과 전체가 캐시됩니다.

| 도구 | 용도 |
|---|---|
| `search_code` · `read_code` · `list_code_files` · `list_prompts` | 코드에서 문구 위치 찾기, 줄 번호 포함 원문 읽기(인용·패치용), 프롬프트 위치 |
| `get_change` · `list_changes` · `get_pattern` | 변경·패턴 상세 조회/필터 |
| `read_document` · `search_documents` | 원본/수정본 문서 내용 조회 (`요구사항정의서!3:10`, `slide 3`, `¶5`, 섹션 제목) |
| `get_code_diff` | 사용자 보완 브랜치 등 코드 버전 비교 hunk |

옵션: `llm.use_tools`, `llm.max_tool_steps`, `llm.max_tool_calls`, `llm.tools`(허용 목록), CLI `--no-tools`.

---

## 직접 툴 추가하기 (플러그인)

부족한 기능은 Python 으로 코딩해 **문서 형식 핸들러** 또는 **GPT 도구**로 등록할 수 있습니다. 예시: [`examples/plugins/sample_plugin.py`](examples/plugins/sample_plugin.py) (JSON 산출물 비교 + 변경 집계 도구).

```python
from result_comp_agent.documents import FlowDocHandler, register_handler
from result_comp_agent.diff.flow_diff import Block, FlowDoc
from result_comp_agent.tools import register_tool

class HwpxHandler(FlowDocHandler):          # 흐름형 문서: extract() 만 구현하면 diff·무결성 검증·GPT 조회가 자동 제공
    name, artifact, extensions = "hwpx", "hwpx", (".hwpx",)
    def extract(self, path) -> FlowDoc: ...

register_handler(HwpxHandler())

@register_tool("lookup_glossary", "사내 표준 용어 조회", {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]})
def lookup_glossary(ctx, term):             # ctx: 코드 인덱스·변경·비교쌍·패턴·코드 diff 접근
    return {"term": term, "standard": "..."}
```

등록 방법: 설정 `plugins: [my_pkg.rca_plugins, ./plugins/hwpx.py]`, 환경변수 `RCA_PLUGINS`, CLI `--plugin`, 또는 패키지 entry point 그룹 `result_comp_agent.plugins`. 확인: `rca tools -c config.yaml`. 표 형식 문서는 `DocumentHandler.compare()` 에서 기존 grid diff 엔진(`diff_grids` + `verify_grid`)을 재사용하면 됩니다.

---

## 구성

```
                    ┌──────────────── Orchestrator ────────────────┐
 원본 문서  ───────▶ │ DiffAgent (결정론, 비교쌍별 프로세스·제한시간) │
 수정본 문서 ──────▶ │   문서 핸들러 → 정렬 → 변경 목록 → 무결성 검증  │
                    │ CodeAgent (결정론)                           │◀── git 저장소 (에이전트 코드)
                    │   코드 인덱스 · 변경→코드 추적 · 브랜치 diff     │
                    │ SupplementAnalystAgent (규칙 + GPT)           │
                    │ Aggregator (결정론)  반복 패턴 · 공통 패턴      │
                    │ ImprovementAdvisorAgent (규칙 + GPT + 도구)    │◀── GPT 도구 레지스트리 (+ 플러그인)
                    │ VerifierAgent (결정론 + GPT 교차검증)          │
                    └──────▶ 실패/strict 판정 → Markdown · Excel · HTML · JSON
```

```
src/result_comp_agent/
  documents/    핸들러 레지스트리 + 기본 핸들러(Excel·PPT·Word·CSV·Markdown·텍스트·PDF)
  extractors/   excel.py, pptx.py, flow.py(Word·MD·TXT·PDF·CSV)
  diff/         normalize, align, grid(표), flow_diff(흐름 문서), excel_diff, pptx_diff, compare(격리·시간제한)
  code/         git_source, indexer, tracer, code_diff
  analysis/     classifier, aggregator, recommender
  llm/          env(.env 로더), client(적응형 GPT 클라이언트·도구 세션), errors, schemas, prompts
  tools/        GPT 도구 레지스트리 · 기본 도구 · 컨텍스트
  agents/       orchestrator, code_agent, supplement_agent, improvement_agent, code_supplement_agent, verifier_agent, grounding
  report/       markdown, excel, html (원자적 쓰기·섹션 격리)
  plugins.py    플러그인 로더
examples/
  pd_agents/    샘플 제품개발 에이전트(시장분석 PPT · 요구사항 Excel · 테스트케이스 Excel)
  build_demo.py 데모 작업공간 생성
  plugins/      플러그인 예시
```

---

## 명령어

```bash
rca run -c config.yaml [--strict] [--strict-on ...] [--fail-fast] [--timeout 300] [--llm-timeout 120] [--llm-budget 1800]
        [--env-file .env] [--offline] [--no-tools] [--plugin my.py] [--formats md,xlsx,html,json]
rca diff 원본 수정본 -o out [--repo URL --ref main --paths agents/x]   # 단일 비교
rca check-env [--probe]                                               # .env·GPT 배포 점검
rca inspect 파일 [--json]                                             # 문서를 어떻게 읽는지 확인
rca tools [-c config.yaml]                                            # 문서 핸들러·GPT 도구(플러그인 포함) 목록
rca code-diff --repo R --base main --head feature/x                   # 코드 버전 비교
```

설정 파일 전체 예시는 [`config/agents.example.yaml`](config/agents.example.yaml) 를 참고하세요.

---

## 보고서

| 파일 | 내용 |
|---|---|
| `report.html` | 단일 파일 대시보드 — 실행 결과/실패 배너, 신뢰도 KPI, 실패 내역, GPT 실행 정보(응답 모델·자동 적응·회로 차단), 에이전트별 권고·패치·변경 검색. 외부 리소스 없음 + CSP, 데이터 이스케이프·해시 기반 DOM id, 섹션별 오류 격리, 대량 행 제한 |
| `report.md` | 같은 내용의 Markdown (코드 펜스 충돌 방지) |
| `report.xlsx` | `요약` `변경사항` `보완패턴` `개선권고` `코드추적` `코드보완` `무결성검증` `실패내역` `GPT도구조회` |
| `analysis.json` | 전체 결과 (status · exit_code · failures · strict 포함) |

모든 보고서는 임시 파일에 쓴 뒤 교체(원자적 쓰기)하며, 한 형식 작성이 실패해도 나머지는 작성되고 실패는 `report` 범주로 기록됩니다.

---

## 한계 (정확한 해석을 위해)

- **무결성 검증의 범위**: 추출한 내용(셀 값·수식 문자열·문단·표·노트·이미지 해시·차트 데이터) 기준으로 변경 목록의 완전성·정확성을 보장합니다. 어떤 행/문단이 어떤 것과 대응되는지의 *의미적* 판단은 대응 방식·점수로 별도 표시됩니다. 시트 속성(데이터 유효성 등)은 값 무결성과 별도로 비교합니다.
- **비교하지 않는 항목**: 엑셀 차트·피벗, PPT 애니메이션·글꼴 세부 서식, Word 머리글/바닥글·텍스트 상자 위치, PDF 서식·이미지(텍스트만). HWP 등 다른 형식은 플러그인으로 추가하세요.
- **코드 추적**: 문자열 리터럴·프롬프트·템플릿 기반입니다. 런타임에 외부(DB·API)에서 오는 이름은 '추적 불가'로 표시됩니다.
- **GPT**: 실제 Azure 호출 경로는 가짜 전송 계층으로 테스트했습니다(오류·적응·도구·캐시 포함). 조직 배포에서는 먼저 `rca check-env --probe` 로 확인하세요.
- **패치**: 인덱스는 줄바꿈을 LF 로 정규화하므로 CRLF 파일에는 `git apply --ignore-whitespace` 가 필요할 수 있습니다.

---

## 개발

```bash
pip install -e ".[dev]"
pytest -q   # 결정론 diff/무결성(전 형식), 코드 추적·git, 검증기, .env 로더, 적응형 GPT 클라이언트(가짜 Azure),
            # 도구 호출·플러그인, 실패/시간초과/strict, HTML 안정성(이스케이프·섹션 격리), CLI
```
