"""가짜 Azure 전송 계층으로 GPT-6 경로 전체를 검증 (환각 응답 포함)."""
from __future__ import annotations

import json
import re

from result_comp_agent.agents.orchestrator import Orchestrator
from result_comp_agent.config import RunConfig
from result_comp_agent.llm.client import LLMClient, LLMError, LLMSettings
from result_comp_agent.report import write_reports


def _handler(helpers, state):
    def h(kw):
        name = helpers.schema_name(kw)
        text = helpers.user_text(kw)
        state.setdefault(name, 0)
        state[name] += 1
        if name == "supplement_analysis":
            items = json.loads(text.split("[변경 목록 JSON]\n", 1)[1])
            out = []
            for it in items:
                cat = it["rule_category"]
                conf = 0.9
                if "p95" in (it.get("after") or ""):
                    cat = "CONTENT_ENRICHMENT"  # 규칙(FACT_CORRECTION)과 다르게 재분류 시도
                out.append({"change_id": it["change_id"], "category": cat, "user_intent": "측정 가능한 기준으로 구체화",
                            "agent_gap": "정량 기준 요구 지침 부재", "confidence": conf})
            return json.dumps({"items": out}, ensure_ascii=False)
        if name == "agent_improvements":
            if "요구사항 정의 에이전트" not in text:
                return json.dumps({"recommendations": []})
            ids = re.findall(r'"(requirements-\d+:C\d{4})"', text)
            risk_ids = [i for i in ids if i.endswith("C0001")]
            recs = [
                {
                    "title": "요구사항 스키마에 리스크 열 추가",
                    "problem": "두 사용자가 모두 리스크 열을 추가",
                    "recommendation": "COLUMNS 에 리스크 추가 및 프롬프트 지침 추가",
                    "category": "STRUCTURE_CHANGE", "priority": "high",
                    "evidence_change_ids": risk_ids, "based_on": ["requirements:R002"],
                    "code_locations": [{"file": "agents/requirements/requirements_agent.py", "line_start": 17, "line_end": 18, "quote": '"우선순위",'}],
                    "proposed_edits": [{"file": "agents/requirements/requirements_agent.py", "original_snippet": '    "우선순위",\n    "담당자",',
                                        "replacement": '    "우선순위",\n    "리스크",\n    "담당자",', "explanation": "리스크 열 추가"}],
                    "expected_effect": "수작업 열 추가 제거",
                },
                {
                    "title": "환각 권고",
                    "problem": "존재하지 않는 근거",
                    "recommendation": "없는 파일 수정",
                    "category": "CONTENT_REWRITE", "priority": "high",
                    "evidence_change_ids": ["requirements-9:C9999"], "based_on": [],
                    "code_locations": [{"file": "agents/requirements/ghost.py", "line_start": 3, "line_end": 3, "quote": "def ghost():"}],
                    "proposed_edits": [], "expected_effect": "",
                },
                {
                    "title": "상세설명 정량 기준 지침",
                    "problem": "응답시간 수치를 사용자들이 정정",
                    "recommendation": "프롬프트에 정량 기준 요구",
                    "category": "FACT_CORRECTION", "priority": "medium",
                    "evidence_change_ids": [i for i in ids if i.endswith("C0004")][:2], "based_on": [],
                    "code_locations": [{"file": "agents/requirements/requirements_agent.py", "line_start": 3, "line_end": 3, "quote": "상세설명은 1~2문장으로 간결하게 작성하세요."}],
                    "proposed_edits": [{"file": "agents/requirements/requirements_agent.py", "original_snippet": "상세설명은 1~2문장으로 간결하게 작성하세요.",
                                        "replacement": "상세설명은 1~2문장으로 작성하되, 성능·용량 등은 측정 기준(예: p95 1초)을 포함하세요.", "explanation": ""}],
                    "expected_effect": "",
                },
            ]
            return json.dumps({"recommendations": recs}, ensure_ascii=False)
        if name == "cross_check":
            items = json.loads(text.split("\n", 1)[1])
            return json.dumps({"results": [{"rec_id": it["rec_id"], "verdict": "supported", "reason": "증거와 일치"} for it in items]}, ensure_ascii=False)
        if name == "code_supplements":
            hunks = re.findall(r"file=(\S+) hunk_new_start=(\d+)", text)
            return json.dumps({"items": [{"file": f, "hunk_new_start": int(n), "symbol": "", "kind": "schema", "summary": "리스크 열 추가"} for f, n in hunks]}, ensure_ascii=False)
        raise AssertionError(name)

    return h


def test_full_pipeline_with_fake_gpt6(demo_ws, tmp_path, helpers, fake_transport_cls):
    state: dict = {}
    transport = fake_transport_cls(_handler(helpers, state))
    llm = LLMClient(LLMSettings(endpoint="https://x.openai.azure.com", api_key="k", cache_dir=str(tmp_path / "llm"), retry_backoff=0), transport=transport)
    cfg = RunConfig.load(demo_ws)
    res = Orchestrator(cfg, llm).run()

    assert res.llm["active"] and res.llm["deployment_main"] == "gpt-6"
    call = transport.completions.calls[0]
    assert call["model"] == "gpt-6" and call["response_format"]["json_schema"]["strict"] is True
    assert call["reasoning_effort"] == "medium" and call["seed"] == 42 and "temperature" not in call
    assert any(c["model"] == "gpt-6-mini" for c in transport.completions.calls)  # 교차검증은 fast 배포

    req = next(a for a in res.agents if a.agent_id == "requirements")
    changes = {c.id: c for comp in req.comparisons for c in comp.changes}
    assert all(c.user_intent for c in changes.values() if not c.format_only)
    reclass = [c for c in changes.values() if c.category_rule and "→LLM" in c.category_rule]
    assert reclass and all(c.category == "CONTENT_ENRICHMENT" for c in reclass)

    llm_recs = {r.title: r for r in req.recommendations + req.rejected_recommendations if r.source == "llm"}
    risk = llm_recs["요구사항 스키마에 리스크 열 추가"]
    assert risk.verification.status == "VERIFIED"
    assert risk.proposed_edits[0].verified and '+    "리스크",' in risk.proposed_edits[0].patch
    assert risk.verification.llm_cross_check == "supported"
    assert llm_recs["환각 권고"].verification.status == "REJECTED"
    assert llm_recs["환각 권고"] in req.rejected_recommendations
    detail = llm_recs["상세설명 정량 기준 지침"]
    assert detail.verification.status == "VERIFIED"
    assert detail.code_refs[0].line_start == 26  # 모델이 준 3행 → 실제 26행으로 보정
    assert detail.proposed_edits[0].verified

    sup = [d for d in req.code_diffs if d.supplements]
    assert sup and all(s.verified for s in sup[0].supplements)
    risk_pattern = next(p for p in req.patterns if p.key.startswith("col_add") and "리스크" in p.title)
    assert risk_pattern.user_code_evidence

    paths = write_reports(res, tmp_path / "out")
    md = open(paths["md"], encoding="utf-8").read()
    assert "환각 권고" in md.split("## 5. 부록")[1]  # 기각된 권고는 부록에만
    assert "```diff" in md

    # 재실행: 동일 입력은 캐시로 재현 (추가 Azure 호출 없음)
    n_calls = len(transport.completions.calls)
    llm2 = LLMClient(LLMSettings(endpoint="https://x.openai.azure.com", api_key="k", cache_dir=str(tmp_path / "llm"), retry_backoff=0), transport=transport)
    res2 = Orchestrator(RunConfig.load(demo_ws), llm2).run()
    assert len(transport.completions.calls) == n_calls and llm2.usage.cache_hits > 0
    r2 = next(a for a in res2.agents if a.agent_id == "requirements")
    assert [r.id for r in r2.recommendations] == [r.id for r in req.recommendations]


def test_validation_feedback_loop(tmp_path, helpers, fake_transport_cls):
    replies = iter(['{"items": [{"change_id": "ghost", "category": "REWORDING", "user_intent": "", "agent_gap": "", "confidence": 1}]}',
                    '{"items": [{"change_id": "ok", "category": "REWORDING", "user_intent": "x", "agent_gap": "y", "confidence": 0.5}]}'])
    t = fake_transport_cls(lambda kw: next(replies))
    llm = LLMClient(LLMSettings(endpoint="e", api_key="k", cache_dir=None, retry_backoff=0), transport=t)
    from result_comp_agent.llm.schemas import SUPPLEMENT_SCHEMA, SupplementM

    def validate(d):
        m = SupplementM.model_validate(d)
        if any(i.change_id != "ok" for i in m.items):
            raise ValueError("입력에 없는 change_id")
        return m

    m = llm.structured(tier="main", system="s", user="u", schema_name="supplement_analysis", schema=SUPPLEMENT_SCHEMA, validate=validate)
    assert m.items[0].change_id == "ok" and llm.usage.retries == 1
    second = t.completions.calls[1]["messages"]
    assert second[-1]["role"] == "user" and "검증에 실패" in second[-1]["content"]


def test_llm_gives_up_after_max_attempts(helpers, fake_transport_cls):
    t = fake_transport_cls(lambda kw: "not json")
    llm = LLMClient(LLMSettings(endpoint="e", api_key="k", cache_dir=None, retry_backoff=0, max_attempts=2), transport=t)
    import pytest

    with pytest.raises(LLMError):
        llm.structured(tier="fast", system="s", user="u", schema_name="x", schema={}, validate=lambda d: d)
    assert len(t.completions.calls) == 2


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-6-prod")
    monkeypatch.setenv("AZURE_OPENAI_TEMPERATURE", "0")
    s = LLMSettings.from_env()
    assert s.is_active and s.deployment_main == "gpt-6-prod" and s.deployment_fast == "gpt-6-mini" and s.temperature == 0.0
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    assert not LLMSettings.from_env().is_active


def test_feedback_retries_then_accepts_last_attempt(fake_transport_cls):
    """근거 문제는 재작성 요청 → 끝까지 남으면 응답은 수용하고 문제 항목만 이후 검증에서 걸러낸다."""
    t = fake_transport_cls(lambda kw: '{"v": 1}')
    llm = LLMClient(LLMSettings(endpoint="e", api_key="k", cache_dir=None, retry_backoff=0, max_attempts=3), transport=t)
    obj = llm.structured(tier="main", system="s", user="u", schema_name="x", schema={}, validate=lambda d: d, feedback=lambda o: ["없는 ID"])
    assert obj == {"v": 1} and len(t.completions.calls) == 3 and llm.usage.retries == 2
    assert "근거 검증에 실패한 항목" in t.completions.calls[1]["messages"][-1]["content"]
    assert llm.usage.log[-1]["unresolved_problems"] == 1
