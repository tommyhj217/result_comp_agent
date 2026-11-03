"""GPT 도구(함수 호출) 레지스트리·기본 도구, CLI 도구 명령."""
from __future__ import annotations

import json
from pathlib import Path

from result_comp_agent.agents.code_agent import CodeAgent
from result_comp_agent.agents.orchestrator import DiffAgent
from result_comp_agent.analysis.aggregator import aggregate
from result_comp_agent.analysis.classifier import classify_all
from result_comp_agent.cli import main
from result_comp_agent.config import RunConfig
from result_comp_agent.tools import ToolRegistry, default_registry
from result_comp_agent.tools.context import ToolContext


def test_registry_error_handling():
    reg = ToolRegistry(max_result_chars=300)
    reg.register_function("echo", "e", {"type": "object", "properties": {"t": {"type": "string"}}, "required": ["t"]}, lambda ctx, t: {"t": t * 100})
    reg.register_function("boom", "b", {}, lambda ctx: 1 / 0)
    assert reg.call("zzz", "{}", None)[1] == "unknown_tool"
    assert reg.call("echo", "{bad", None)[1] == "bad_arguments"
    assert reg.call("echo", "{}", None)[1] == "bad_arguments"
    out, err = reg.call("boom", "{}", None)
    assert err == "tool_error" and "ZeroDivisionError" in out
    out, err = reg.call("echo", '{"t": "가나다", "extra": 1}', None)
    data = json.loads(out)  # 잘려도 항상 유효한 JSON
    assert err is None and data["truncated"] is True
    assert {s["function"]["name"] for s in reg.specs()} == {"echo", "boom"}


def _ctx(demo_ws):
    cfg = RunConfig.load(demo_ws)
    agent = next(a for a in cfg.agents if a.id == "requirements")
    comps, _ = DiffAgent(cfg, []).run(agent, cfg.pairs_for("requirements"), lambda f: None)
    changes = {c.id: c for comp in comps for c in comp.changes}
    classify_all(list(changes.values()))
    repo, index, err, _ = CodeAgent(cfg.resolve(cfg.cache_dir)).load(agent, cfg.resolve(agent.repo))
    return ToolContext(agent_id="requirements", index=index, changes=changes, comparisons=comps, patterns=aggregate("requirements", comps))


def test_builtin_tools(demo_ws):
    ctx = _ctx(demo_ws)
    reg = default_registry()
    call = lambda n, a: json.loads(reg.call(n, json.dumps(a, ensure_ascii=False), ctx)[0])
    hits = call("search_code", {"query": "DEFAULT_OWNER"})["results"]
    assert any(h["file"].endswith("requirements_agent.py") for h in hits) or call("search_code", {"query": "기획팀"})["results"]
    code = call("read_code", {"file": "requirements_agent.py", "start_line": 12, "end_line": 14})
    assert code["file"] == "agents/requirements/requirements_agent.py" and '   13|     "요구사항ID",' in code["code"]
    assert any(p["symbol"] == "SYSTEM_PROMPT" for p in call("list_prompts", {})["prompts"])
    ch = call("list_changes", {"text": "p95"})
    assert ch["total"] >= 1
    one = call("get_change", {"change_id": ch["changes"][0]["id"]})
    assert one["after"] and "p95" in one["after"]
    doc = call("read_document", {"pair_id": "requirements-1", "side": "revised", "locator": "요구사항정의서!3:4"})
    assert [u["locator"] for u in doc["units"]] == ["요구사항정의서!3", "요구사항정의서!4"] and "리스크" in doc["units"][0]["text"]
    found = call("search_documents", {"query": "AES-256"})
    assert found["results"][0]["pair_id"] == "requirements-1" and found["results"][0]["side"] == "revised"
    assert "error" in call("get_change", {"change_id": "nope"})
    assert call("get_pattern", {"pattern_id": ctx.patterns[0].id})["id"] == ctx.patterns[0].id


def test_cli_check_env(tmp_path, capsys):
    (tmp_path / ".env").write_text("AZURE_OPENAI_ENDPOINT=https://r.openai.azure.com\nAZURE_OPENAI_API_KEY=secretsecret\nAZURE_OPENAI_DEPLOYMENT=gpt-4o\nAZUTE_OPEN_API_VERSION=2024-10-21\n", encoding="utf-8")
    assert main(["check-env", "--env-file", str(tmp_path / ".env")]) == 0
    out = capsys.readouterr().out
    assert "gpt-4o" in out and "secretsecret" not in out and "AZUTE_OPEN_API_VERSION" in out
    (tmp_path / "bad.env").write_text("AZURE_OPENAI_ENDPOINT=https://r.openai.azure.com\n", encoding="utf-8")
    assert main(["check-env", "--env-file", str(tmp_path / "bad.env")]) == 7


def test_cli_inspect_and_tools(demo_ws, capsys):
    ws = Path(demo_ws).parent
    assert main(["inspect", str(ws / "outputs/시장분석보고서_agent.pptx"), "--limit", "5"]) == 0
    assert "slide 1" in capsys.readouterr().out
    assert main(["inspect", "nothing.hwp"]) == 3
    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "word" in out and "read_document" in out and "search_code" in out


def test_run_offline_end_to_end(demo_ws, tmp_path):
    out = tmp_path / "out"
    assert main(["run", "-c", str(demo_ws), "--offline", "-o", str(out), "--strict"]) == 0
    data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert data["status"] == "SUCCESS" and data["exit_code"] == 0
    rel = data["reliability"]
    assert rel["integrity"]["pass"] == rel["integrity"]["pairs"] == 5
    agents = {a["agent_id"]: a for a in data["agents"]}
    titles = [r["title"] for r in agents["requirements"]["recommendations"]]
    assert any("'리스크' 열 추가" in t for t in titles) and any("하드코딩된 값 '기획팀'" in t for t in titles)
    assert any("'가격 전략' 섹션" in r["title"] for r in agents["market_analysis"]["recommendations"])
    from openpyxl import load_workbook

    wb = load_workbook(out / "report.xlsx")
    assert {"요약", "변경사항", "보완패턴", "개선권고", "코드추적", "코드보완", "무결성검증", "실패내역", "GPT도구조회"} <= set(wb.sheetnames)


def test_run_llm_enabled_without_env_is_config_error(demo_ws, tmp_path):
    cfg_path = Path(demo_ws)
    text = cfg_path.read_text(encoding="utf-8").replace("enabled: auto", "enabled: true")
    p = cfg_path.parent / "cfg_llm_true.yaml"
    p.write_text(text, encoding="utf-8")
    assert main(["run", "-c", str(p), "-o", str(tmp_path / "o"), "--env-file", str(tmp_path / "missing.env")]) == 7  # 지정한 .env 없음
    (tmp_path / "empty.env").write_text("# 비어 있음\n", encoding="utf-8")
    assert main(["run", "-c", str(p), "-o", str(tmp_path / "o"), "--strict", "--env-file", str(tmp_path / "empty.env")]) == 7
    rc = main(["run", "-c", str(p), "-o", str(tmp_path / "o2"), "--env-file", str(tmp_path / "empty.env")])
    data = json.loads((tmp_path / "o2" / "analysis.json").read_text(encoding="utf-8"))
    assert rc == 0 and any(f["category"] == "config" for f in data["failures"]) and data["status"] == "PARTIAL"


def test_single_diff_command(demo_ws, tmp_path):
    ws = Path(demo_ws).parent
    out = tmp_path / "d"
    rc = main(["diff", str(ws / "outputs/요구사항정의서_agent.xlsx"), str(ws / "user_final/요구사항정의서_김민수.xlsx"), "-o", str(out),
               "--offline", "--repo", str(ws / "pd_agents_repo"), "--ref", "main", "--paths", "agents/requirements", "--cache-dir", str(tmp_path / "c")])
    assert rc == 0
    data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert data["agents"][0]["trace_stats"]["traced"] > 0
