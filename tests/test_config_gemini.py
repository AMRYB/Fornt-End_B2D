from __future__ import annotations

import pytest

from agentic_core.config import GEMINI_AGENT_NAMES, Settings


def gemini_keys() -> dict[str, str]:
    return {
        "gemini_discovery_api_key": "key-discovery",
        "gemini_requirements_api_key": "key-requirements",
        "gemini_architecture_api_key": "key-architecture",
        "gemini_database_api_key": "key-database",
        "gemini_api_api_key": "key-api",
        "gemini_devops_api_key": "key-devops",
        "gemini_reviewer_api_key": "key-reviewer",
    }


def test_gemini_api_key_for_each_agent(tmp_path):
    settings = Settings(
        _env_file=None, llm_provider="gemini", data_dir=tmp_path, **gemini_keys()
    )

    assert [
        settings.gemini_api_key_for(name) for name in GEMINI_AGENT_NAMES
    ] == [
        "key-discovery",
        "key-requirements",
        "key-architecture",
        "key-database",
        "key-api",
        "key-devops",
        "key-reviewer",
    ]
    settings.check_credentials()


def test_gemini_credentials_require_all_seven_distinct_keys(tmp_path):
    missing = gemini_keys()
    missing["gemini_api_api_key"] = ""
    settings = Settings(
        _env_file=None, llm_provider="gemini", data_dir=tmp_path, **missing
    )
    with pytest.raises(RuntimeError, match="GEMINI_API_API_KEY"):
        settings.check_credentials()

    duplicate = gemini_keys()
    duplicate["gemini_reviewer_api_key"] = duplicate["gemini_devops_api_key"]
    settings = Settings(
        _env_file=None, llm_provider="gemini", data_dir=tmp_path, **duplicate
    )
    with pytest.raises(RuntimeError, match="must be distinct"):
        settings.check_credentials()


def test_gemini_model_default_and_overrides(tmp_path):
    settings = Settings(
        _env_file=None, llm_provider="gemini", data_dir=tmp_path, **gemini_keys()
    )
    assert settings.effective_model() == "gemini-2.5-flash"

    configured = settings.model_copy(
        update={"gemini_model": "gemini-flash-configured"}
    )
    assert configured.effective_model() == "gemini-flash-configured"

    explicit = settings.model_copy(update={"llm_model": "gemini-explicit"})
    assert explicit.effective_model() == "gemini-explicit"


def test_unknown_gemini_agent_is_rejected(tmp_path):
    settings = Settings(
        _env_file=None, llm_provider="gemini", data_dir=tmp_path, **gemini_keys()
    )
    with pytest.raises(ValueError, match="Unknown Gemini agent"):
        settings.gemini_api_key_for("spare")
