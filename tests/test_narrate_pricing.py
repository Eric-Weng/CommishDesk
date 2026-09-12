"""Story 4.6 — ``commishdesk/narrate/pricing.py``: the dated static price table
and the worst-case per-call cost estimator.

One test (or group) per behaviour the spec calls out: the worst-case
``estimate_cost_usd`` math (char-proxy input tokens, full ``max_output_tokens``
ceiling on output — never an average), the unknown-model fail-closed raise, the
``is_pricing_stale`` boundary, and the AD-1 import-fence (no ``httpx``, no SDK).
"""

from __future__ import annotations

import ast
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from commishdesk.errors import CostCeilingExceededError
from commishdesk.llmconfig import _DEFAULT_FALLBACK, _DEFAULT_PRIMARY, LLMModelConfig
from commishdesk.narrate.pricing import (
    MODEL_PRICES,
    PRICING_REVIEW_INTERVAL_DAYS,
    PRICING_UPDATED,
    ModelPrice,
    estimate_cost_usd,
    is_pricing_stale,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    """Exactly the char-proxy formula: input = ceil(len/4), output = the full
    ceiling — both priced against the table, never an average actual."""
    price = MODEL_PRICES["anthropic:claude-sonnet-5"]
    payload = "x" * 4000  # exactly 1000 tokens at 4 chars/token
    cost = estimate_cost_usd(payload, _ANTHROPIC, max_output_tokens=1000)
    expected = (1000 / 1000) * price.input_usd_per_1k + (1000 / 1000) * price.output_usd_per_1k
    assert cost == pytest.approx(expected)


def test_estimate_cost_usd_rounds_input_tokens_up() -> None:
    """``ceil``, not floor or truncate — one extra character buys one extra
    priced token, isolated here with ``max_output_tokens=0``."""
    price = MODEL_PRICES["anthropic:claude-sonnet-5"]
    payload = "x" * 4001  # just over 1000 tokens -> ceil(1000.25) == 1001
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
