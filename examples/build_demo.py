"""데모 작업공간 생성기.

1) examples/pd_agents 를 git 저장소로 만들고(main) 사용자 보완 브랜치(feature/kim-risk-column)를 추가
2) 에이전트를 실행해 원본 산출물(PPT/Excel) 생성 (Azure 미설정 시 샘플 콘텐츠)
3) 사용자들이 산출물을 보완한 '최종본'을 시뮬레이션
4) 분석 설정 파일(config.yaml) 작성

사용:  python examples/build_demo.py [작업공간 경로]   →   rca run -c <작업공간>/config.yaml
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import PatternFill
from pptx import Presentation

HERE = Path(__file__).resolve().parent
BRIEF = {"product": "플랜잇(AI 일정관리 앱)", "team": "신사업기획팀", "goal": "AI 기반 개인 일정 추천 앱 MVP"}


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.com", GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.com")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(ws: Path) -> Path:
    repo = ws / "pd_agents_repo"
    shutil.copytree(HERE / "pd_agents", repo, ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("common", "agents", "agents/planning", "agents/requirements", "agents/testing"):  # 패키지화
        (repo / d / "__init__.py").touch()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "제품개발 프로세스 에이전트 v1")
    # 사용자(김민수)가 요구사항 에이전트 코드에 직접 '리스크' 열을 보완한 브랜치
    git(repo, "checkout", "-q", "-b", "feature/kim-risk-column")
    p = repo / "agents/requirements/requirements_agent.py"
    src = p.read_text(encoding="utf-8")
    src = src.replace('    "우선순위",\n', '    "우선순위",\n    "리스크",\n', 1)
    src = src.replace(
        "상세설명은 1~2문장으로 간결하게 작성하세요.\n",
        "상세설명은 1~2문장으로 간결하게 작성하세요.\n리스크는 높음/중간/낮음 중 하나로, 판단 근거를 함께 고려해 작성하세요.\n",
        1,
    )
    p.write_text(src, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "요구사항 정의서에 리스크 열 추가 (김민수)")
    git(repo, "checkout", "-q", "main")
    return repo


def generate_outputs(repo: Path, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    os.environ["PD_AGENTS_OFFLINE"] = "1"  # 사용자 보완 시뮬레이션이 고정 콘텐츠를 전제하므로 데모 생성은 항상 오프라인
    sys.path.insert(0, str(repo))
    try:
        ma = importlib.import_module("agents.planning.market_analysis_agent")
        rq = importlib.import_module("agents.requirements.requirements_agent")
        tp = importlib.import_module("agents.testing.test_plan_agent")
        return {
            "market_analysis": Path(ma.run(BRIEF, str(out / "시장분석보고서_agent.pptx"))),
            "requirements": Path(rq.run(BRIEF, str(out / "요구사항정의서_agent.xlsx"))),
            "test_plan": Path(tp.run(BRIEF, str(out / "테스트케이스_agent.xlsx"))),
        }
    finally:
        sys.path.remove(str(repo))


# ----------------------------------------------------------------------------- 사용자 보완 시뮬레이션
YELLOW = PatternFill("solid", fgColor="FFFF00")


def _row_of(ws, key: str) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(r, 1).value == key:
            return r
    raise KeyError(key)


def revise_requirements_kim(src: Path, dst: Path) -> None:
    wb = load_workbook(src)
    ws = wb.active
    ws.insert_cols(6)  # '우선순위'(E) 뒤에 '리스크'
    ws.cell(3, 6).value = "리스크"
    for key, risk in zip(["REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005", "REQ-006"], ["중간", "높음", "높음", "중간", "낮음", "낮음"]):
        ws.cell(_row_of(ws, key), 6).value = risk
    ws.cell(_row_of(ws, "REQ-002"), 4).value = "소셜 계정(카카오/네이버/애플)으로 로그인하며, 최초 로그인 시 약관 동의를 받는다."
    ws.cell(_row_of(ws, "REQ-003"), 5).value = "상"
    ws.cell(_row_of(ws, "REQ-005"), 4).value = "주요 화면 응답시간 1초 이내 (p95 기준)."
    ws.cell(_row_of(ws, "REQ-006"), 7).value = "모바일팀"
    ws.append(["REQ-007", "비기능", "개인정보 암호화", "개인정보는 AES-256으로 암호화하여 저장한다.", "상", "높음", "보안팀"])
    ws.cell(_row_of(ws, "REQ-001"), 4).comment = Comment("본인인증 절차 추가 필요 (법무 검토)", "김민수")
    for r in range(4, ws.max_row + 1):
        if ws.cell(r, 5).value == "상":
            ws.cell(r, 5).fill = YELLOW
    wb.save(dst)


def revise_requirements_lee(src: Path, dst: Path) -> None:
    wb = load_workbook(src)
    ws = wb.active
    ws.insert_cols(6)
    ws.cell(3, 6).value = "리스크"
    for key, risk in zip(["REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005", "REQ-006"], ["낮음", "중간", "높음", "높음", "중간", "낮음"]):
        ws.cell(_row_of(ws, key), 6).value = risk
    ws.cell(3, 7).value = "담당 조직"
    ws.cell(3, 8).value = "수용 기준"
    criteria = {
        "REQ-001": "이메일 인증 후 가입 완료",
        "REQ-002": "카카오/네이버 로그인 성공률 99%",
        "REQ-003": "추천 수락률 30% 이상",
        "REQ-004": "5분 이내 양방향 동기화",
        "REQ-005": "p95 1초 이내",
    }
    for key, v in criteria.items():
        ws.cell(_row_of(ws, key), 8).value = v
    ws.cell(_row_of(ws, "REQ-005"), 4).value = "주요 화면 응답시간 1초 이내."
    ws.delete_rows(_row_of(ws, "REQ-006"))
    ws.append(["REQ-007", "비기능", "개인정보 보호", "개인정보 수집 최소화 및 암호화 저장.", "상", "높음", "보안팀", "개인정보 영향평가 통과"])
    wb.save(dst)


def _slide_by_title(prs, title: str):
    for s in prs.slides:
        if s.shapes.title is not None and s.shapes.title.text == title:
            return s
    raise KeyError(title)


def _add_slide_after(prs, after_title: str, title: str, bullets: list[str]) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    tf = slide.placeholders[1].text_frame
    tf.text = bullets[0]
    for b in bullets[1:]:
        tf.add_paragraph().text = b
    ids = prs.slides._sldIdLst
    items = list(ids)
    pos = [i for i, s in enumerate(prs.slides) if s.shapes.title is not None and s.shapes.title.text == after_title][0]
    ids.remove(items[-1])
    ids.insert(pos + 1, items[-1])


def _set_para(slide, idx: int, text: str) -> None:
    p = slide.placeholders[1].text_frame.paragraphs[idx]
    p.runs[0].text = text
    for r in p.runs[1:]:
        r.text = ""


def revise_market_kim(src: Path, dst: Path) -> None:
    prs = Presentation(src)
    s = _slide_by_title(prs, "시장 개요")
    _set_para(s, 0, "국내 일정관리 앱 시장 규모 약 1,850억원 (2025년, 통계청)")
    s.placeholders[1].text_frame.add_paragraph().text = "출처: 통계청 2025 모바일 서비스 이용 실태조사"
    t = [sh for sh in _slide_by_title(prs, "경쟁사 분석").shapes if sh.has_table][0].table
    t.cell(2, 2).text = "복잡한 UI, 모바일 앱 평점 3.2"
    _add_slide_after(prs, "타겟 고객", "가격 전략", ["기본 무료 + 프리미엄 월 4,900원", "연간 결제 시 20% 할인", "팀 요금제 1인당 월 3,900원"])
    _slide_by_title(prs, "타겟 고객").notes_slide.notes_text_frame.text = "타겟 고객 근거: 사용자 인터뷰 20건 (2025.10)"
    prs.save(dst)


def revise_market_park(src: Path, dst: Path) -> None:
    prs = Presentation(src)
    s = _slide_by_title(prs, "시장 개요")
    _set_para(s, 1, "연평균 성장률 11.5% (2023~2025)")
    s.placeholders[1].text_frame.add_paragraph().text = "출처: 한국모바일산업협회 2025"
    _add_slide_after(prs, "타겟 고객", "가격 전략", ["프리미엄 월 5,900원 (경쟁사 대비 중간 가격대)", "학생 할인 30%"])
    c = _slide_by_title(prs, "결론 및 제언")
    tf = c.placeholders[1].text_frame
    p = tf.paragraphs[1]._p
    p.getparent().remove(p)
    prs.save(dst)


def revise_test_plan_lee(src: Path, dst: Path) -> None:
    wb = load_workbook(src)
    ws = wb.active
    ws.cell(_row_of(ws, "TC-004"), 5).value = "양방향 동기화, 5분 이내 반영"
    ws.cell(_row_of(ws, "TC-005"), 5).value = "p95 응답시간 1초 이내"
    ws.append(["TC-006", "REQ-001", "중복 이메일로 회원가입 시도", "기가입 이메일", "가입 거부 및 안내 메시지 표시", "상"])
    ws.append(["TC-007", "REQ-002", "소셜 로그인 중 권한 동의 취소", "소셜 계정 보유", "로그인 화면으로 복귀, 오류 없음", "중"])
    ws.append(["TC-008", "REQ-004", "구글 계정 연동 해제 후 동기화", "연동 해제 상태", "동기화 중단 및 재연동 안내", "중"])
    wb.save(dst)


CONFIG = """# 데모 분석 설정 — build_demo.py 가 생성
project: "플랜잇 제품개발 에이전트 산출물 분석 (데모)"
output_dir: ./rca_output
cache_dir: ./.rca_cache
# Azure OpenAI 접속 정보는 이 폴더(또는 현재 폴더)의 .env 에서 읽습니다:
#   AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT / AZURE_OPENAI_API_VERSION
llm:
  enabled: auto            # auto: .env 설정이 완전하면 GPT 사용, 없으면 규칙 기반
  cross_check: true
  use_tools: true
diff:
  timeout_seconds: 300
agents:
  - id: market_analysis
    name: 시장분석 에이전트
    stage: "1. 기획"
    output_type: pptx
    repo: ./pd_agents_repo
    ref: main
    paths: [agents/planning, common]
  - id: requirements
    name: 요구사항 정의 에이전트
    stage: "2. 요구사항 정의"
    output_type: xlsx
    repo: ./pd_agents_repo
    ref: main
    paths: [agents/requirements, common]
    code_compare:
      - base: main
        head: feature/kim-risk-column
        label: 김민수 보완 브랜치
  - id: test_plan
    name: 테스트케이스 에이전트
    stage: "5. 테스트"
    output_type: xlsx
    repo: ./pd_agents_repo
    ref: main
    paths: [agents/testing, common]
comparisons:
  - {agent: market_analysis, baseline: outputs/시장분석보고서_agent.pptx, revised: user_final/시장분석보고서_김민수.pptx, revised_by: 김민수}
  - {agent: market_analysis, baseline: outputs/시장분석보고서_agent.pptx, revised: user_final/시장분석보고서_박서준.pptx, revised_by: 박서준}
  - {agent: requirements, baseline: outputs/요구사항정의서_agent.xlsx, revised: user_final/요구사항정의서_김민수.xlsx, revised_by: 김민수}
  - {agent: requirements, baseline: outputs/요구사항정의서_agent.xlsx, revised: user_final/요구사항정의서_이지은.xlsx, revised_by: 이지은}
  - {agent: test_plan, baseline: outputs/테스트케이스_agent.xlsx, revised: user_final/테스트케이스_이지은.xlsx, revised_by: 이지은}
"""


def build(ws: Path) -> Path:
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    repo = make_repo(ws)
    outs = generate_outputs(repo, ws / "outputs")
    uf = ws / "user_final"
    uf.mkdir()
    revise_market_kim(outs["market_analysis"], uf / "시장분석보고서_김민수.pptx")
    revise_market_park(outs["market_analysis"], uf / "시장분석보고서_박서준.pptx")
    revise_requirements_kim(outs["requirements"], uf / "요구사항정의서_김민수.xlsx")
    revise_requirements_lee(outs["requirements"], uf / "요구사항정의서_이지은.xlsx")
    revise_test_plan_lee(outs["test_plan"], uf / "테스트케이스_이지은.xlsx")
    cfg = ws / "config.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    return cfg


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_workspace").resolve()
    cfg = build(target)
    print(f"데모 작업공간 생성 완료: {target}\n다음 명령으로 분석하세요:\n  rca run -c {cfg}")
