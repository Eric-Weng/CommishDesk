"""Story 3.2 — ``commishdesk/llmconfig.py``: engine-level LLM model config.

Where a provider/model swap lives so it is config, not code. Every case passes an
explicit mapping so the real ``os.environ`` is never read.
"""

from __future__ import annotations

import os

import pytest

from commishdesk.errors import CommishDeskError, NarratorError
from commishdesk.llmconfig import LLMConfig, LLMModelConfig, load_llm_config


def test_defaults_when_env_is_empty() -> None:
    cfg = load_llm_config({})
    assert cfg == LLMConfig(
        primary=LLMModelConfig("anthropic", "claude-sonnet-5"),
        fallback=LLMModelConfig("google", "gemini-3.5-flash"),
    )
    assert cfg.primary.endpoint is None
    assert cfg.fallback.endpoint is None


def test_primary_and_fallback_model_overrides() -> None:
    cfg = load_llm_config(
        {
            "COMMISHDESK_LLM_PRIMARY": "anthropic:claude-haiku-4-5",
            "COMMISHDESK_LLM_FALLBACK": "google:gemini-2.5-flash",
        }
    )
    assert cfg.primary == LLMModelConfig("anthropic", "claude-haiku-4-5")
    assert cfg.fallback == LLMModelConfig("google", "gemini-2.5-flash")


def test_endpoint_overrides() -> None:
    cfg = load_llm_config(
        {
            "COMMISHDESK_LLM_PRIMARY_ENDPOINT": "https://gw.example/anthropic",
            "COMMISHDESK_LLM_FALLBACK_ENDPOINT": "https://gw.example/google",
        }
    )
    assert cfg.primary.endpoint == "https://gw.example/anthropic"
    assert cfg.fallback.endpoint == "https://gw.example/google"


def test_matrix_row_config_override_haiku_fallback() -> None:
    cfg = load_llm_config({"COMMISHDESK_LLM_FALLBACK": "anthropic:claude-haiku-4-5"})
    assert cfg.fallback == LLMModelConfig("anthropic", "claude-haiku-4-5")
    assert cfg.primary == LLMModelConfig("anthropic", "claude-sonnet-5")  # untouched


@pytest.mark.parametrize(
    "spec",
    ["openai:gpt-4o", "litellm:whatever", "openrouter:x", "anthropic", "  :  ", "google:"],
)
def test_bad_or_unknown_provider_spec_raises_narrator_error(spec: str) -> None:
    with pytest.raises(NarratorError):
        load_llm_config({"COMMISHDESK_LLM_PRIMARY": spec})
    with pytest.raises(NarratorError):
        load_llm_config({"COMMISHDESK_LLM_FALLBACK": spec})


def test_narrator_error_is_a_commishdesk_error() -> None:
    assert issubclass(NarratorError, CommishDeskError)


def test_blank_values_fall_back_to_defaults() -> None:
    cfg = load_llm_config(
        {"COMMISHDESK_LLM_PRIMARY": "   ", "COMMISHDESK_LLM_PRIMARY_ENDPOINT": ""}
    )
    assert cfg.primary == LLMModelConfig("anthropic", "claude-sonnet-5")
    assert cfg.primary.endpoint is None


def test_os_environ_is_neither_read_nor_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMISHDESK_LLM_PRIMARY", "google:should-be-ignored")
    before = dict(os.environ)
    cfg = load_llm_config({})
    assert cfg.primary.provider == "anthropic"  # the explicit mapping wins
    assert dict(os.environ) == before


def test_model_config_is_frozen() -> None:
    cfg = load_llm_config({})
    with pytest.raises((AttributeError, TypeError)):
        cfg.primary.model_id = "x"  # type: ignore[misc]
