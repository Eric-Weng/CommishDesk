"""Story 4.6 — ``commishdesk/narrate/pricing.py``: the dated static price table
and the worst-case per-call cost estimator.

One test (or group) per behaviour the spec calls out: the worst-case
``estimate_cost_usd`` math (char-proxy input tokens, full ``max_output_tokens``
ceiling on output — never an average), the unknown-model fail-closed raise, the
``is_pricing_stale`` boundary, and the AD-1 import-fence (no ``httpx``, no SDK).
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from commishdesk.errors import CostCeilingExceededError
from commishdesk.llmconfig import (
    _DEFAULT_FALLBACK,
    _DEFAULT_PRIMARY,
    LLMConfig,
    LLMModelConfig,
    load_llm_config,
)
from commishdesk.narrate.pricing import (
    CHARS_PER_TOKEN,
    MAX_BILLABLE_NARRATION_ATTEMPTS,
    MAX_BILLABLE_VERIFICATION_CALLS,
    MODEL_PRICES,
    PRICING_REVIEW_INTERVAL_DAYS,
    PRICING_UPDATED,
    ModelPrice,
    config_for_level,
    estimate_cost_usd,
    is_pricing_stale,
    league_worst_case_usd,
    narration_worst_case_usd,
)
from tests.conftest import REPO_ROOT

_ANTHROPIC = LLMModelConfig("anthropic", "claude-sonnet-5")
_GOOGLE = LLMModelConfig("google", "gemini-3.5-flash")


# --------------------------------------------------------------------------- #
# the price table itself
# --------------------------------------------------------------------------- #


def test_model_prices_covers_the_two_llmconfig_defaults() -> None:
    """At minimum the two ``llmconfig`` defaults must be priced (Boundaries).
    Asserted against ``llmconfig``'s own default constants, not re-typed
    literals, so a future default-model change surfaces here instead of only
    as a runtime ``CostCeilingExceededError`` for whoever hits it."""
    assert _DEFAULT_PRIMARY in MODEL_PRICES
    assert _DEFAULT_FALLBACK in MODEL_PRICES


def test_model_prices_match_the_provider_price_pages_literally() -> None:
    """Retro finding F3 (Epic 4): every other pricing test reads its expected
    value back out of MODEL_PRICES itself, so a wrong entry (input/output
    confused, a stale rate) would pass the whole suite silently. This asserts
    the real, current numbers directly, taken from each provider's own
    pricing page (2026-09-13) rather than reconstructed from the table under
    test. google:gemini-3.5-flash was actually WRONG in this table before
    this date -- $0.075 / $0.30 per 1k, roughly 20x / 30x under real price
    ($1.50 / $9.00 per 1M) -- caught only by comparing against the source,
    not by any test already in this file."""
    sonnet = MODEL_PRICES["anthropic:claude-sonnet-5"]
    assert sonnet.input_usd_per_1k == pytest.approx(0.003)
    assert sonnet.output_usd_per_1k == pytest.approx(0.015)

    flash_35 = MODEL_PRICES["google:gemini-3.5-flash"]
    assert flash_35.input_usd_per_1k == pytest.approx(0.0015)
    assert flash_35.output_usd_per_1k == pytest.approx(0.009)

    flash_38 = MODEL_PRICES["google:gemini-3.8-flash"]
    assert flash_38.input_usd_per_1k == pytest.approx(0.00075)
    assert flash_38.output_usd_per_1k == pytest.approx(0.00375)


def test_pricing_updated_is_a_valid_iso_date_and_interval_is_positive() -> None:
    date.fromisoformat(PRICING_UPDATED)  # does not raise
    assert PRICING_REVIEW_INTERVAL_DAYS > 0


def test_model_price_is_frozen() -> None:
    price = ModelPrice(input_usd_per_1k=1.0, output_usd_per_1k=2.0)
    with pytest.raises((AttributeError, TypeError)):
        price.input_usd_per_1k = 5.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# estimate_cost_usd — worst-case math
# --------------------------------------------------------------------------- #


def test_estimate_cost_usd_worst_case_math() -> None:
    """Exactly the char-proxy formula: input = ceil(len / CHARS_PER_TOKEN), output = the full
    ceiling — both priced against the table, never an average actual."""
    price = MODEL_PRICES["anthropic:claude-sonnet-5"]
    payload = "x" * int(1000 * CHARS_PER_TOKEN)  # exactly 1000 tokens
    cost = estimate_cost_usd(payload, _ANTHROPIC, max_output_tokens=1000)
    expected = (1000 / 1000) * price.input_usd_per_1k + (1000 / 1000) * price.output_usd_per_1k
    assert cost == pytest.approx(expected)


def test_estimate_cost_usd_rounds_input_tokens_up() -> None:
    """``ceil``, not floor or truncate — one extra character buys one extra
    priced token, isolated here with ``max_output_tokens=0``."""
    price = MODEL_PRICES["anthropic:claude-sonnet-5"]
    payload = "x" * (int(1000 * CHARS_PER_TOKEN) + 1)  # just over 1000 tokens -> 1001
    cost = estimate_cost_usd(payload, _ANTHROPIC, max_output_tokens=0)
    assert cost == pytest.approx((1001 / 1000) * price.input_usd_per_1k)


def test_estimate_cost_usd_empty_payload_has_zero_input_cost() -> None:
    price = MODEL_PRICES["google:gemini-3.5-flash"]
    cost = estimate_cost_usd("", _GOOGLE, max_output_tokens=100)
    assert cost == pytest.approx((100 / 1000) * price.output_usd_per_1k)


def test_estimate_scales_with_the_max_output_tokens_ceiling_not_actual_output() -> None:
    """The estimator never sees a real completion — a larger *ceiling* alone
    must raise the estimate; this is the "worst case, not expected value"
    contract in isolation."""
    small = estimate_cost_usd("a modest payload", _ANTHROPIC, max_output_tokens=100)
    large = estimate_cost_usd("a modest payload", _ANTHROPIC, max_output_tokens=8192)
    assert large > small


def test_estimate_cost_usd_default_max_output_tokens_is_documented() -> None:
    """The bare default (no ``max_output_tokens=`` passed) still produces a
    positive, finite estimate — callers pricing a real narration call should
    still pass ``narrate.llm._MAX_OUTPUT_TOKENS`` explicitly rather than rely on
    this default matching it by coincidence (Spec Change Log, Loopback 1)."""
    cost = estimate_cost_usd("payload", _ANTHROPIC)
    assert cost > 0


# --------------------------------------------------------------------------- #
# unknown model -> fail closed
# --------------------------------------------------------------------------- #


def test_unknown_model_raises_cost_ceiling_exceeded_naming_the_model() -> None:
    unpriced = LLMModelConfig("anthropic", "claude-nope-9000")
    with pytest.raises(CostCeilingExceededError) as excinfo:
        estimate_cost_usd("payload", unpriced)
    assert "anthropic:claude-nope-9000" in str(excinfo.value)


def test_unknown_provider_google_model_also_fails_closed() -> None:
    unpriced = LLMModelConfig("google", "gemini-nope")
    with pytest.raises(CostCeilingExceededError) as excinfo:
        estimate_cost_usd("payload", unpriced)
    assert "google:gemini-nope" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# is_pricing_stale — the boundary, via the _today seam
# --------------------------------------------------------------------------- #


def test_is_pricing_stale_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    from commishdesk.narrate import pricing

    updated = date.fromisoformat(PRICING_UPDATED)

    monkeypatch.setattr(
        pricing, "_today", lambda: updated + timedelta(days=PRICING_REVIEW_INTERVAL_DAYS)
    )
    assert is_pricing_stale() is False  # exactly at the interval: not yet stale

    monkeypatch.setattr(
        pricing, "_today", lambda: updated + timedelta(days=PRICING_REVIEW_INTERVAL_DAYS + 1)
    )
    assert is_pricing_stale() is True  # one day past: stale


def test_is_pricing_stale_false_immediately_after_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from commishdesk.narrate import pricing

    monkeypatch.setattr(pricing, "_today", lambda: date.fromisoformat(PRICING_UPDATED))
    assert is_pricing_stale() is False


# --------------------------------------------------------------------------- #
# the pricing names are re-exported eagerly from commishdesk.narrate (I4)
# --------------------------------------------------------------------------- #


def test_pricing_names_are_reexported_from_narrate_package() -> None:
    import commishdesk.narrate as narrate_pkg

    assert narrate_pkg.estimate_cost_usd is estimate_cost_usd
    assert narrate_pkg.is_pricing_stale is is_pricing_stale
    assert narrate_pkg.MODEL_PRICES is MODEL_PRICES
    assert narrate_pkg.ModelPrice is ModelPrice


def test_importing_narrate_package_pulls_in_no_sdk_or_httpx() -> None:
    """``narrate/pricing.py`` is eagerly imported (like ``safety.py``) — it must
    not be the module that breaks I4's "import commishdesk.narrate pulls in no
    SDK / httpx" guarantee. A fresh subprocess, not ``sys.modules`` surgery in
    this process, so the check is order-independent of every other test."""
    import subprocess

    prog = (
        "import commishdesk.narrate\n"
        "import sys\n"
        "bad = {'httpx', 'anthropic', 'google.genai'} & sys.modules.keys()\n"
        "assert not bad, sorted(bad)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --------------------------------------------------------------------------- #
# AD-1 import fence
# --------------------------------------------------------------------------- #


def test_pricing_module_imports_only_stdlib_and_commishdesk() -> None:
    tree = ast.parse(
        (REPO_ROOT / "commishdesk" / "narrate" / "pricing.py").read_text(encoding="utf-8")
    )
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    external = roots - sys.stdlib_module_names
    assert external <= {"commishdesk"}, external
    assert "httpx" not in roots


def test_chars_per_token_is_conservative_against_measured_billing() -> None:
    """2026-09-13: ~4.28M characters sent, 1.5M input tokens billed = 2.85
    characters a token. A worst-case estimator must never price input at fewer
    tokens than the provider actually bills."""
    assert 0 < CHARS_PER_TOKEN <= 2.85


# --------------------------------------------------------------------------- #
# Story 7.2 -- the payload-free worst case (AD-45, AD-46)
# --------------------------------------------------------------------------- #


def _longest_prompt() -> str:
    from commishdesk.voices.beat_writer import (
        BEAT_WRITER,
        BEAT_WRITER_WEEKLY,
        BEAT_WRITER_WEEKLY_COLD_START,
    )

    return max(
        (v.system_prompt for v in (BEAT_WRITER, BEAT_WRITER_WEEKLY, BEAT_WRITER_WEEKLY_COLD_START)),
        key=len,
    )


def _expected_worst_case(config: LLMConfig) -> float:
    from commishdesk.facts.schema import NARRATION_TOKEN_CAP
    from commishdesk.narrate.llm import MAX_OUTPUT_TOKENS
    from commishdesk.narrate.verify import EXTRACTOR_VOICE

    payload = "x" * NARRATION_TOKEN_CAP + _longest_prompt()
    one = estimate_cost_usd(payload, config.primary, max_output_tokens=MAX_OUTPUT_TOKENS)
    two = estimate_cost_usd(payload, config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS)
    total = (one + two) * 2
    if config.verifier is not None:
        verifier_input = "x" * int(MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN) + EXTRACTOR_VOICE.system_prompt
        total += estimate_cost_usd(verifier_input, config.verifier, max_output_tokens=MAX_OUTPUT_TOKENS)
    return total


def test_call_count_constants_have_one_definition() -> None:
    import commishdesk.cli as cli_mod
    import commishdesk.weekly as weekly_mod

    assert MAX_BILLABLE_NARRATION_ATTEMPTS == 2
    assert MAX_BILLABLE_VERIFICATION_CALLS == 1
    assert cli_mod._MAX_BILLABLE_NARRATION_ATTEMPTS is MAX_BILLABLE_NARRATION_ATTEMPTS
    assert cli_mod._MAX_BILLABLE_VERIFICATION_CALLS is MAX_BILLABLE_VERIFICATION_CALLS
    assert weekly_mod.MAX_BILLABLE_NARRATION_ATTEMPTS is MAX_BILLABLE_NARRATION_ATTEMPTS
    for mod in (cli_mod, weekly_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        assigned = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(t, ast.Name)
        }
        assert not {"_MAX_BILLABLE_NARRATION_ATTEMPTS", "_MAX_BILLABLE_VERIFICATION_CALLS"} & assigned


@pytest.mark.parametrize("level", ["normal", "no_new_activations"])
def test_billing_levels_price_primary_fallback_and_verifier(level: str) -> None:
    config = load_llm_config({})
    assert config.verifier is not None
    got = league_worst_case_usd(level)
    assert got > 0.0
    assert got == pytest.approx(_expected_worst_case(config))
    assert league_worst_case_usd(level, config) == got


def test_budget_model_prices_the_fallback_in_both_slots() -> None:
    config = load_llm_config({})
    swapped = dataclasses.replace(config, primary=config.fallback)
    assert league_worst_case_usd("budget_model", config) == pytest.approx(
        league_worst_case_usd("normal", swapped)
    )
    assert config_for_level(config, "budget_model") == swapped
    assert config_for_level(config, "normal") is config


@pytest.mark.parametrize("level", ["template", "discord_only", "stop"])
def test_no_llm_levels_cost_nothing(level: str) -> None:
    assert league_worst_case_usd(level) == 0.0
    assert config_for_level(load_llm_config({}), level) is None


def test_no_verifier_drops_the_verifier_term() -> None:
    config = load_llm_config({})
    bare = dataclasses.replace(config, verifier=None)
    with_verifier = league_worst_case_usd("normal", config)
    without = league_worst_case_usd("normal", bare)
    assert 0.0 < without < with_verifier
    assert without == pytest.approx(_expected_worst_case(bare))


@pytest.mark.parametrize("level", ["bogus", "", "Normal"])
def test_unknown_level_raises_value_error_naming_it(level: str) -> None:
    with pytest.raises(ValueError, match=repr(level)):
        league_worst_case_usd(level)
    with pytest.raises(ValueError):
        config_for_level(load_llm_config({}), level)


def test_unpriced_model_still_fails_closed() -> None:
    config = dataclasses.replace(
        load_llm_config({}), primary=LLMModelConfig("anthropic", "not-a-priced-model")
    )
    with pytest.raises(CostCeilingExceededError):
        league_worst_case_usd("normal", config)
    with pytest.raises(CostCeilingExceededError):
        league_worst_case_usd("budget_model", dataclasses.replace(config, fallback=config.primary))


def test_worst_case_is_an_upper_bound_on_any_real_payload_estimate() -> None:
    """The CLI prices the real payload with the same helper; the payload-free figure
    must never be lower, even for a very large league."""
    from commishdesk.facts.schema import NARRATION_TOKEN_CAP
    from commishdesk.narrate import build_narration_payload
    from tests.test_invariants import _i3_narration

    config = load_llm_config({})
    voice_prompt = _longest_prompt()
    bound = league_worst_case_usd("normal", config)
    for n_teams in (4, 12, 150):
        payload = build_narration_payload(_i3_narration(n_teams))
        assert len(payload) <= NARRATION_TOKEN_CAP
        assert narration_worst_case_usd(payload, voice_prompt, config) <= bound
