""".env 기반 Azure OpenAI 설정 로딩."""
from __future__ import annotations

import pytest

from result_comp_agent.llm.client import LLMSettings
from result_comp_agent.llm.env import find_env_file, normalize_endpoint, parse_dotenv, resolve_azure_env

ENV = """# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://my-res.openai.azure.com/
AZURE_OPENAI_API_KEY="abc123secretkey"   # 키
export AZURE_OPENAI_DEPLOYMENT='gpt-4o-prod'
AZUTE_OPEN_API_VERSION=2024-10-21
"""


def test_parse_dotenv_forms():
    d = parse_dotenv('A=1\nexport B="x y" # c\nC=\'q\'\nD=v # comment\n# skip\nE="line1\\nline2"\nF="multi\nline"\n')
    assert d == {"A": "1", "B": "x y", "C": "q", "D": "v", "E": "line1\nline2", "F": "multi\nline"}


def test_resolve_four_keys_with_typo_alias(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    env = resolve_azure_env(config_dir=str(tmp_path), environ={})
    assert env.complete, env.errors
    assert env.get("AZURE_OPENAI_ENDPOINT") == "https://my-res.openai.azure.com"
    assert env.get("AZURE_OPENAI_API_KEY") == "abc123secretkey"
    assert env.get("AZURE_OPENAI_DEPLOYMENT") == "gpt-4o-prod"
    assert env.get("AZURE_OPENAI_API_VERSION") == "2024-10-21"
    assert env.values["AZURE_OPENAI_API_VERSION"].alias == "AZUTE_OPEN_API_VERSION"
    assert any("AZUTE_OPEN_API_VERSION" in w for w in env.warnings)
    masked = {r["key"]: r for r in env.masked()}
    assert masked["AZURE_OPENAI_API_KEY"]["value"] == "abc1…tkey" and masked["AZURE_OPENAI_API_KEY"]["source"] == ".env"

    s = LLMSettings.from_env(config_dir=str(tmp_path), environ={})
    assert s.is_active and s.deployment_main == "gpt-4o-prod" and s.fast == "gpt-4o-prod" and s.api_version == "2024-10-21"
    assert s.env_report.env_file == str((tmp_path / ".env").resolve())


def test_precedence_env_over_dotenv_and_override(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    environ = {"AZURE_OPENAI_DEPLOYMENT": "from-os"}
    assert resolve_azure_env(config_dir=str(tmp_path), environ=environ).get("AZURE_OPENAI_DEPLOYMENT") == "from-os"
    assert resolve_azure_env(config_dir=str(tmp_path), environ=environ, override=True).get("AZURE_OPENAI_DEPLOYMENT") == "gpt-4o-prod"


def test_missing_values_reported(tmp_path):
    (tmp_path / ".env").write_text("AZURE_OPENAI_ENDPOINT=https://x.openai.azure.com\n", encoding="utf-8")
    env = resolve_azure_env(config_dir=str(tmp_path), environ={})
    assert not env.complete
    assert any("AZURE_OPENAI_API_KEY" in e for e in env.errors) and any("AZURE_OPENAI_DEPLOYMENT" in e for e in env.errors)
    s = LLMSettings.from_env(config_dir=str(tmp_path), environ={})
    assert not s.is_active and set(s.missing()) == {"AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"}


def test_full_url_pasted_as_endpoint(tmp_path):
    base, ext, warns = normalize_endpoint("https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview")
    assert base == "https://r.openai.azure.com" and ext == {"api_version": "2024-08-01-preview", "deployment": "gpt-4o"} and warns
    (tmp_path / ".env").write_text("AZURE_OPENAI_ENDPOINT=https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview\nAZURE_OPENAI_API_KEY=k\n", encoding="utf-8")
    env = resolve_azure_env(config_dir=str(tmp_path), environ={})
    assert env.complete and env.get("AZURE_OPENAI_DEPLOYMENT") == "gpt-4o" and env.get("AZURE_OPENAI_API_VERSION") == "2024-08-01-preview"
    assert normalize_endpoint("https://r.openai.azure.com/openai/v1/")[1] == {"mode": "v1"}


def test_explicit_env_file_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_env_file(str(tmp_path / "nope.env"))
