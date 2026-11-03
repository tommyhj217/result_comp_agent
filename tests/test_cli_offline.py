from __future__ import annotations

import json
from pathlib import Path

from result_comp_agent.cli import main


def test_run_offline_end_to_end(demo_ws, tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    out = tmp_path / "out"
    assert main(["run", "-c", str(demo_ws), "--offline", "-o", str(out), "--strict"]) == 0
    data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    rel = data["reliability"]
    assert rel["integrity"]["pass"] == rel["integrity"]["pairs"] == 5
    assert rel["recommendations"]["REJECTED"] == 0
    agents = {a["agent_id"]: a for a in data["agents"]}
    req_titles = [r["title"] for r in agents["requirements"]["recommendations"]]
    assert any("'리스크' 열 추가" in t for t in req_titles)
    assert any("하드코딩된 값 '기획팀'" in t for t in req_titles)
    ma_titles = [r["title"] for r in agents["market_analysis"]["recommendations"]]
    assert any("'가격 전략' 섹션" in t for t in ma_titles)
    for f in ("report.md", "report.xlsx", "report.html"):
        assert (out / f).stat().st_size > 1000
    from openpyxl import load_workbook

    wb = load_workbook(out / "report.xlsx")
    assert {"요약", "변경사항", "보완패턴", "개선권고", "코드추적", "코드보완", "무결성검증"} <= set(wb.sheetnames)


def test_single_diff_command(demo_ws, tmp_path):
    ws = Path(demo_ws).parent
    out = tmp_path / "d"
    rc = main(["diff", str(ws / "outputs/요구사항정의서_agent.xlsx"), str(ws / "user_final/요구사항정의서_김민수.xlsx"), "-o", str(out),
               "--offline", "--repo", str(ws / "pd_agents_repo"), "--ref", "main", "--paths", "agents/requirements", "--cache-dir", str(tmp_path / "c")])
    assert rc == 0
    data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert data["agents"][0]["trace_stats"]["traced"] > 0
