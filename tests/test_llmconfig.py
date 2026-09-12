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
        primary=LLMModelConfig("anthropic", "claude-sonnet-5", timeout=60.0),
        fallback=LLMModelConfig("google", "gemini-3.5-flash", timeout=60.0),
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
    assert cfg.primary == LLMModelConfig("anthropic", "claude-haiku-4-5", timeout=60.0)
    assert cfg.fallback == LLMModelConfig("google", "gemini-2.5-flash", timeout=60.0)


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
    assert cfg.fallback == LLMModelConfig("anthropic", "claude-haiku-4-5", timeout=60.0)
    assert cfg.primary == LLMModelConfig(
        "anthropic", "claude-sonnet-5", timeout=60.0
    )  # untouched


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
        {
            "COMMISHDESK_LLM_PRIMARY": "   ",
            "COMMISHDESK_LLM_PRIMARY_ENDPOINT": "",
            "COMMISHDESK_LLM_TIMEOUT": "  ",
        }
    )
    assert cfg.primary == LLMModelConfig("anthropic", "claude-sonnet-5", timeout=60.0)
    assert cfg.primary.endpoint is None


def test_timeout_defaults_to_sixty_seconds() -> None:
    cfg = load_llm_config({})
    assert cfg.primary.timeout == 60.0
    assert cfg.fallback.timeout == 60.0


def test_timeout_override_applies_to_both_roles() -> None:
    cfg = load_llm_config({"COMMISHDESK_LLM_TIMEOUT": "30"})
    assert cfg.primary.timeout == 30.0
    assert cfg.fallback.timeout == 30.0


def test_timeout_at_the_upper_bound_is_accepted() -> None:
    cfg = load_llm_config({"COMMISHDESK_LLM_TIMEOUT": "600"})
    assert cfg.primary.timeout == 600.0
    assert cfg.fallback.timeout == 600.0


@pytest.mark.parametrize(
    "bad",
    ["soon", "nope", "0", "-5", "0.0", "nan", "inf", "1e999", "601", "1e9"],
)
def test_invalid_timeout_raises_narrator_error(bad: str) -> None:
    with pytest.raises(NarratorError):
        load_llm_config({"COMMISHDESK_LLM_TIMEOUT": bad})


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


# --------------------------------------------------------------------------- #
# Story 4.6 — COMMISHDESK_COST_CEILING_USD
# --------------------------------------------------------------------------- #


def test_cost_ceiling_defaults_to_one_dollar() -> None:
    cfg = load_llm_config({})
    assert cfg.cost_ceiling_usd == 1.00


def test_cost_ceiling_override() -> None:
    cfg = load_llm_config({"COMMISHDESK_COST_CEILING_USD": "5.50"})
    assert cfg.cost_ceiling_usd == 5.50


def test_cost_ceiling_blank_falls_back_to_default() -> None:
    cfg = load_llm_config({"COMMISHDESK_COST_CEILING_USD": "  "})
    assert cfg.cost_ceiling_usd == 1.00


@pytest.mark.parametrize("bad", ["soon", "nope", "0", "-5", "0.0", "nan", "inf", "-inf"])
def test_invalid_cost_ceiling_raises_narrator_error(bad: str) -> None:
    with pytest.raises(NarratorError):
        load_llm_config({"COMMISHDESK_COST_CEILING_USD": bad})


def test_cost_ceiling_has_no_upper_bound() -> None:
    """Unlike ``COMMISHDESK_LLM_TIMEOUT``, a cost ceiling has no FR-40-style
    multi-hour-stall concern to bound against."""
    cfg = load_llm_config({"COMMISHDESK_COST_CEILING_USD": "1000000"})
    assert cfg.cost_ceiling_usd == 1_000_000.0


def test_llm_config_constructed_without_cost_ceiling_matches_the_loader_default() -> None:
    """A pre-Story-4.6 direct ``LLMConfig(primary=..., fallback=...)`` (the
    ``test_narrate_llm.py`` ``CONFIG`` fixture, e.g.) must stay equal to
    ``load_llm_config({})`` — the new field's dataclass default has to match the
    loader's parsed default exactly."""
    cfg = load_llm_config({})
    assert LLMConfig(primary=cfg.primary, fallback=cfg.fallback) == cfg
