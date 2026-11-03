"""가짜 Azure 전송 계층으로 GPT 경로 전체를 검증 (도구 호출·환각 응답·캐시 포함)."""
from __future__ import annotations

import json
import re

from result_comp_agent.agents.orchestrator import Orchestrator
from result_comp_agent.config import RunConfig
from result_comp_agent.llm.client import LLMClient
from result_comp_agent.report import write_reports


def _handler(helpers, state):
    def h(kw):
        name = helpers.schema_name(kw)
        text = helpers.user_text(kw)
        state.setdefault(name, 0)
        state[name] += 1
        if "tools" in kw:  # 도구 조회 세션
            n_tool_msgs = sum(1 for m in kw["messages"] if m.get("role") == "tool")
            if "요구사항 정의 에이전트" in text and n_tool_msgs == 0:
                return {"tool_calls": [
                    helpers.tool_call("c1", "search_code", {"query": "상세설명"}),
                    helpers.tool_call("c2", "read_code", {"file": "agents/requirements/requirements_agent.py", "start_line": 10, "end_line": 30}),
                    helpers.tool_call("c3", "list_changes", {"text": "p95"}),
                ]}
            return "조사 완료"
        if name == "supplement_analysis":
            items = json.loads(text.split("[변경 목록 JSON]\n", 1)[1])
            out = []
            for it in items:
                cat = it["rule_category"]
                if "p95" in (it.get("after") or ""):
                    cat = "CONTENT_ENRICHMENT"  # 규칙(FACT_CORRECTION)과 다르게 재분류 시도
                out.append({"change_id": it["change_id"], "category": cat, "user_intent": "측정 가능한 기준으로 구체화",
                            "agent_gap": "정량 기준 요구 지침 부재", "confidence": 0.9})
            return json.dumps({"items": out}, ensure_ascii=False)
        if name == "agent_improvements":
            if "요구사항 정의 에이전트" not in text:
                return json.dumps({"recommendations": []})
            state["saw_tool_transcript"] = "[GPT 도구 조회 기록" in text
            ids = re.findall(r'"(requirements-\d+:C\d{4})"', text)
            recs = [
                {
                    "title": "요구사항 스키마에 리스크 열 추가", "problem": "두 사용자가 모두 리스크 열을 추가",
                    "recommendation": "COLUMNS 에 리스크 추가 및 프롬프트 지침 추가", "category": "STRUCTURE_CHANGE", "priority": "high",
                    "evidence_change_ids": [i for i in ids if i.endswith("C0001")], "based_on": ["requirements:R002"],
                    "code_locations": [{"file": "agents/requirements/requirements_agent.py", "line_start": 17, "line_end": 18, "quote": '"우선순위",'}],
                    "proposed_edits": [{"file": "agents/requirements/requirements_agent.py", "original_snippet": '    "우선순위",\n    "담당자",',
                                        "replacement": '    "우선순위",\n    "리스크",\n    "담당자",', "explanation": "리스크 열 추가"}],
                    "expected_effect": "수작업 열 추가 제거",
                },
                {
                    "title": "환각 권고", "problem": "존재하지 않는 근거", "recommendation": "없는 파일 수정", "category": "CONTENT_REWRITE", "priority": "high",
                    "evidence_change_ids": ["requirements-9:C9999"], "based_on": [],
                    "code_locations": [{"file": "agents/requirements/ghost.py", "line_start": 3, "line_end": 3, "quote": "def ghost():"}],
                    "proposed_edits": [], "expected_effect": "",
                },
                {
                    "title": "상세설명 정량 기준 지침", "problem": "응답시간 수치를 사용자들이 정정", "recommendation": "프롬프트에 정량 기준 요구",
                    "category": "FACT_CORRECTION", "priority": "medium", "evidence_change_ids": [i for i in ids if i.endswith("C0004")][:2], "based_on": [],
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


def test_full_pipeline_with_fake_gpt(demo_ws, tmp_path, helpers, fake_transport_cls, settings_factory):
    state: dict = {}
    transport = fake_transport_cls(_handler(helpers, state))
    llm = LLMClient(settings_factory(), transport=transport)
    res = Orchestrator(RunConfig.load(demo_ws), llm).run()

    assert res.status == "SUCCESS" and res.exit_code == 0, res.failures
    assert res.llm["active"] and res.llm["deployment_main"] == "gpt-4o"
    assert res.llm["served_models"]["gpt-4o"] == ["gpt-4o-2024-08-06"]
    call = next(c for c in transport.completions.calls if "response_format" in c)
    assert call["model"] == "gpt-4o" and call["response_format"]["json_schema"]["strict"] is True
    assert call["seed"] == 42 and "temperature" not in call and "reasoning_effort" not in call
    assert "max_completion_tokens" in call and "timeout" in call

    req = next(a for a in res.agents if a.agent_id == "requirements")
    # 도구 조회가 실행되고, 그 기록이 최종 구조화 호출에 전달됨
    assert [t["tool"] for t in req.tool_trace] == ["search_code", "read_code", "list_changes"]
    assert all(t["error"] is None for t in req.tool_trace)
    assert state["saw_tool_transcript"] is True

    changes = {c.id: c for comp in req.comparisons for c in comp.changes}
    assert all(c.user_intent for c in changes.values() if not c.format_only)
    reclass = [c for c in changes.values() if c.category_rule and "→LLM" in c.category_rule]
    assert reclass and all(c.category == "CONTENT_ENRICHMENT" for c in reclass)

    llm_recs = {r.title: r for r in req.recommendations + req.rejected_recommendations if r.source == "llm"}
    risk = llm_recs["요구사항 스키마에 리스크 열 추가"]
    assert risk.verification.status == "VERIFIED"
    assert risk.proposed_edits[0].verified and '+    "리스크",' in risk.proposed_edits[0].patch
    assert risk.verification.llm_cross_check == "supported"
    assert llm_recs["환각 권고"].verification.status == "REJECTED" and llm_recs["환각 권고"] in req.rejected_recommendations
    detail = llm_recs["상세설명 정량 기준 지침"]
    assert detail.verification.status == "VERIFIED"
    assert detail.code_refs[0].line_start == 26  # 모델이 준 3행 → 실제 26행으로 보정
    assert detail.proposed_edits[0].verified

    sup = [d for d in req.code_diffs if d.supplements]
    assert sup and all(s.verified for s in sup[0].supplements)
    assert next(p for p in req.patterns if p.key.startswith("col_add") and "리스크" in p.title).user_code_evidence

    paths, errors = write_reports(res, tmp_path / "out")
    assert not errors
    md = open(paths["md"], encoding="utf-8").read()
    assert "환각 권고" in md.split("## 5. 부록")[1]
    assert "```diff" in md and "GPT 도구 조회 기록" in md

    # 재실행: 동일 입력은 캐시로 재현 (추가 Azure 호출 없음)
    n_calls = len(transport.completions.calls)
    llm2 = LLMClient(settings_factory(), transport=transport)
    res2 = Orchestrator(RunConfig.load(demo_ws), llm2).run()
    assert len(transport.completions.calls) == n_calls and llm2.usage.cache_hits > 0
    r2 = next(a for a in res2.agents if a.agent_id == "requirements")
    assert [r.id for r in r2.recommendations] == [r.id for r in req.recommendations]
    assert [t["tool"] for t in r2.tool_trace] == ["search_code", "read_code", "list_changes"]


def test_gpt_timeouts_are_reported_as_failures_and_strict_exit(demo_ws, helpers, fake_transport_cls, settings_factory):
    t = fake_transport_cls(lambda kw: helpers.APITimeoutError("Request timed out."))
    llm = LLMClient(settings_factory(max_attempts=2, cache_dir=None), transport=t)
    cfg = RunConfig.load(demo_ws)
    res = Orchestrator(cfg, llm).run()
    llm_fail = [f for f in res.failures if f.category == "llm"]
    assert llm_fail and all(f.kind == "timeout" for f in llm_fail)
    stages = {f.stage for f in llm_fail}
    assert {"llm.supplement", "llm.improvement:tools"} <= stages
    assert res.status == "PARTIAL" and res.exit_code == 0  # 비 strict: 규칙 기반 결과로 완주
    assert all(a.recommendations for a in res.agents)  # 규칙 기반 권고는 그대로 제공
    # strict → 종료코드 4
    cfg2 = RunConfig.load(demo_ws)
    cfg2.strict.enabled = True
    res2 = Orchestrator(cfg2, LLMClient(settings_factory(max_attempts=1, cache_dir=None), transport=t)).run()
    assert res2.exit_code == 4 and "llm" in res2.strict["violated"]


def test_deployment_not_found_opens_circuit(demo_ws, helpers, fake_transport_cls, settings_factory):
    t = fake_transport_cls(lambda kw: helpers.FakeAPIError(404, "The API deployment for this resource does not exist.", code="DeploymentNotFound"))
    llm = LLMClient(settings_factory(cache_dir=None), transport=t)
    res = Orchestrator(RunConfig.load(demo_ws), llm).run()
    assert len(t.completions.calls) == 1  # 회로 차단 → 이후 호출 없음
    kinds = [f.kind for f in res.failures if f.category == "llm"]
    assert kinds[0] == "not_found" and set(kinds[1:]) <= {"circuit_open"}
    assert "배포" in res.failures[0].message or "DEPLOYMENT" in res.failures[0].message
