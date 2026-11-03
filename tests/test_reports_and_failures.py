"""보고서 안정성(HTML 이스케이프·섹션 격리·원자적 쓰기) + 실패 표시 + strict 처리."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from result_comp_agent.agents.orchestrator import Orchestrator
from result_comp_agent.config import PairSpec, RunConfig
from result_comp_agent.report import html_report, write_reports


@pytest.fixture
def offline_result(demo_ws):
    return Orchestrator(RunConfig.load(demo_ws), None).run()


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            if k == "id":
                self.ids.append(v)


def test_html_escapes_hostile_content_and_uses_safe_ids(offline_result):
    res = offline_result
    res.project = "<script>alert('p')</script>"
    a = res.agents[0]
    a.agent_id = "x'\"><img src=x onerror=alert(1)>"
    a.name = "</h2><script>alert(2)</script>"
    a.comparisons[0].changes[0].after = "</td></tr><script>alert(3)</script>"
    a.comparisons[0].changes[0].location.path = "<b onmouseover=alert(4)>"
    out = html_report.render_html(res)
    assert "<script>alert" not in out and "onerror=alert" not in out.replace("onerror=alert(1)&gt;", "")
    p = _Tags()
    p.feed(out)
    assert p.tags.count("script") == 1 and p.tags.count("img") == 0
    assert all(re.fullmatch(r"[a-z]+-[0-9a-f]{10}", i) for i in p.ids)
    assert "Content-Security-Policy" in out


def test_html_section_isolation(offline_result, monkeypatch):
    def boom(a):
        raise RuntimeError("렌더링 실패 테스트")

    monkeypatch.setattr(html_report, "_agent_section", boom)
    out = html_report.render_html(offline_result)
    assert out.count("섹션을 표시하지 못했습니다") == len(offline_result.agents)
    assert "에이전트 요약" in out and out.rstrip().endswith("</html>")


def test_report_write_failure_is_isolated(offline_result, tmp_path, monkeypatch):
    monkeypatch.setattr(html_report, "render_html", lambda res: (_ for _ in ()).throw(MemoryError("x")))
    paths, errors = write_reports(offline_result, tmp_path / "o")
    assert set(paths) == {"md", "xlsx", "json"} and errors[0].stage == "report.html"
    data = json.loads((tmp_path / "o" / "analysis.json").read_text(encoding="utf-8"))
    assert any(f["stage"] == "report.html" for f in data["failures"])
    assert not any(p.name.endswith(".tmp") for p in (tmp_path / "o").iterdir())


def _cfg_with_bad_pairs(demo_ws, tmp_path):
    cfg = RunConfig.load(demo_ws)
    (tmp_path / "corrupt.xlsx").write_bytes(b"broken")
    cfg.comparisons += [
        PairSpec(agent="requirements", baseline="outputs/요구사항정의서_agent.xlsx", revised=str(tmp_path / "nope.xlsx"), revised_by="최"),
        PairSpec(agent="requirements", baseline="outputs/요구사항정의서_agent.xlsx", revised=str(tmp_path / "corrupt.xlsx"), revised_by="정"),
    ]
    return cfg


def test_comparison_failures_reported_and_strict(demo_ws, tmp_path):
    cfg = _cfg_with_bad_pairs(demo_ws, tmp_path)
    res = Orchestrator(cfg, None).run()
    req = next(a for a in res.agents if a.agent_id == "requirements")
    assert {f.kind for f in req.failed_comparisons} == {"file_not_found", "parse_error"}
    assert res.status == "PARTIAL" and res.exit_code == 0
    assert res.reliability["comparisons"] == {"configured": 7, "succeeded": 5, "failed": 2, "failed_detail": res.reliability["comparisons"]["failed_detail"]}
    html = html_report.render_html(res)
    assert "비교 실패" in html and "파일 없음" in html and "문서 읽기 실패" in html

    cfg2 = _cfg_with_bad_pairs(demo_ws, tmp_path)
    cfg2.strict.enabled = True
    res2 = Orchestrator(cfg2, None).run()
    assert res2.exit_code == 3 and res2.strict["violated"] == ["comparison"]

    cfg3 = _cfg_with_bad_pairs(demo_ws, tmp_path)
    cfg3.strict.enabled, cfg3.strict.fail_on = True, ["integrity"]
    assert Orchestrator(cfg3, None).run().exit_code == 0

    cfg4 = _cfg_with_bad_pairs(demo_ws, tmp_path)
    cfg4.strict.enabled, cfg4.strict.fail_fast = True, True
    res4 = Orchestrator(cfg4, None).run()
    assert res4.status == "FAILED" and res4.exit_code == 3 and res4.strict["aborted"]["stage"] == "diff"
    assert [a.agent_id for a in res4.agents][-1] == "requirements"  # 중단 이후 에이전트는 실행되지 않음


def test_comparison_timeout_marks_failure(demo_ws):
    cfg = RunConfig.load(demo_ws)
    cfg.comparisons = cfg.comparisons[:1]
    orch = Orchestrator(cfg, None, diff_target="slow_target:compare_slow")
    cfg.diff.timeout_seconds = 0.5
    res = orch.run()
    fc = res.agents[0].failed_comparisons
    assert fc and fc[0].kind == "timeout"
    assert res.status == "FAILED" and res.exit_code == 1


def test_code_load_failure_is_reported(demo_ws):
    cfg = RunConfig.load(demo_ws)
    cfg.agents[0].ref = "no-such-branch"
    res = Orchestrator(cfg, None).run()
    f = next(x for x in res.failures if x.category == "code")
    assert f.kind == "ref_not_found" and f.stage == "code.load"
