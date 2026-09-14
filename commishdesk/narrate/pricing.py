"""Story 4.6 — a dated, static price table plus a worst-case per-call cost
estimator, so ``commishdesk/cli.py`` can price a narration attempt before it
happens (FR-21's "one command ... tells me what it cost").

:func:`estimate_cost_usd` is a single-call estimator: it prices *one* call to
*one* model against *one* payload. The multiplier for "how many billable
attempts can one run actually make" (the ``regenerate`` tier's one re-narration)
and "which of the two selectable models" (primary vs. fallback) both live at the
``cli.py`` call site — not here (Spec Change Log, Loopback 1).

**Worst-case, not expected-value.** ``input_tokens = ceil(len(payload) / CHARS_PER_TOKEN)`` — the
same char-proxy approximation style as ``facts.schema.NARRATION_TOKEN_CAP`` — and
output is priced at the full ``max_output_tokens`` ceiling the caller passes,
never an average actual. This is a ceiling on what a call *could* cost, not a
prediction of what it *will*.

**Fail closed on an unpriced model.** A ``model`` whose ``"<provider>:<model_id>"``
key (the same spec string :mod:`commishdesk.llmconfig` parses) has no
:data:`MODEL_PRICES` entry raises :class:`~commishdesk.errors.CostCeilingExceededError`
— a silent skip would let an unpriced model narrate for free of any cost check.

**Dated, not silently stale.** :data:`PRICING_UPDATED` plus
:func:`is_pricing_stale` turn "someone forgot to refresh the price table" into
one observable signal an operator can act on; staleness never blocks a run —
the cost ceiling is the gate, not this.

**Deliberate carve-out from the no-model-literal invariant (Story 3.2).** That
invariant (``tests/test_narrate_llm.py::test_config_override_changes_no_narrate_file``)
exists so a model swap stays a config-only change; this module necessarily names
the two ``llmconfig`` defaults literally so an unpriced model can fail closed *by
name*. Swapping ``COMMISHDESK_LLM_PRIMARY`` / ``_FALLBACK`` to an unpriced model
now also needs a one-line edit here.

Import fence (AD-1): stdlib + :mod:`commishdesk.errors` +
:class:`commishdesk.llmconfig.LLMModelConfig` only — no ``httpx``, no SDK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from commishdesk.errors import CostCeilingExceededError
from commishdesk.llmconfig import LLMModelConfig

__all__ = [
    "MODEL_PRICES",
    "PRICING_REVIEW_INTERVAL_DAYS",
    "PRICING_UPDATED",
    "ModelPrice",
    "estimate_cost_usd",
    "is_pricing_stale",
]


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-1,000-token USD list pricing for one model, input and output priced
    separately (provider pricing is never symmetric)."""

    input_usd_per_1k: float
    output_usd_per_1k: float


#: Keyed ``"<provider>:<model_id>"`` — the same spec string
#: ``commishdesk.llmconfig`` parses from ``COMMISHDESK_LLM_PRIMARY`` /
#: ``_FALLBACK``. Covers at minimum the two ``llmconfig`` defaults; any other
#: model a deployment points at must be added here or it fails closed
#: (:class:`~commishdesk.errors.CostCeilingExceededError`) in
#: :func:`estimate_cost_usd`.
MODEL_PRICES: dict[str, ModelPrice] = {
    # Sonnet-class list pricing: ~$3 / ~$15 per *million* tokens, i.e. $0.003 /
    # $0.015 per 1k.
    "anthropic:claude-sonnet-5": ModelPrice(
        input_usd_per_1k=0.003, output_usd_per_1k=0.015
    ),
    # CORRECTED 2026-09-13, direct from Google's own pricing page (paid tier,
    # per 1M tokens): $1.50 input / $9.00 output (output explicitly "including
    # thinking tokens"). The previous entry here ($0.075 / $0.30) understated
    # real cost by roughly 20x on input and 30x on output -- the cost-ceiling
    # gate had been passing runs at a fraction of their true price risk for
    # this provider. Per 1k: $0.0015 / $0.009.
    "google:gemini-3.5-flash": ModelPrice(
        input_usd_per_1k=0.0015, output_usd_per_1k=0.009
    ),
    # Added 2026-09-13, direct from Google's pricing page: introductory rate
    # through 2026-12-31 is $0.75 / $3.75 per 1M (output "including thinking
    # tokens"), i.e. $0.00075 / $0.00375 per 1k. Doubles to $1.50 / $7.50 per
    # 1M ($0.0015 / $0.0075 per 1k) starting 2027-01-01 -- re-check before
    # then; PRICING_REVIEW_INTERVAL_DAYS alone (90 days) will not catch a
    # calendar-fixed rate change on its own.
    "google:gemini-3.8-flash": ModelPrice(
        input_usd_per_1k=0.00075, output_usd_per_1k=0.00375
    ),
}

#: ISO date the table above was last checked against live provider pricing.
PRICING_UPDATED = "2026-09-13"

#: How many days may pass before an un-refreshed table logs a staleness warning.
PRICING_REVIEW_INTERVAL_DAYS = 90


def _today() -> date:
    """The current UTC date. A seam: :func:`is_pricing_stale` reads through this
    function (not ``datetime.now`` inline) so a test can force staleness with
    ``monkeypatch.setattr(pricing, "_today", ...)`` instead of waiting out the
    real review interval."""
    return datetime.now(tz=UTC).date()


def is_pricing_stale() -> bool:
    """``True`` once :data:`PRICING_UPDATED` is more than
    :data:`PRICING_REVIEW_INTERVAL_DAYS` days in the past.

    An operator signal only — the caller logs one warning and keeps going; a
    stale table never blocks a run (the cost ceiling is the gate, not this).
    """
    updated = date.fromisoformat(PRICING_UPDATED)
    return (_today() - updated).days > PRICING_REVIEW_INTERVAL_DAYS


#: Characters of payload per estimated input token. Measured, not assumed: on
#: 2026-09-13 a day of live runs sent ~4.28M characters (Facts JSON plus the
#: system prompt) and was billed 1.5M input tokens — 2.85 characters a token,
#: because dense JSON (short keys, digits, punctuation) tokenizes far tighter
#: than English prose's ~4. The old ``/ 4`` under-priced input by ~1.4x. 2.5
#: keeps this a worst-case bound rather than an average.
CHARS_PER_TOKEN = 2.5


def estimate_cost_usd(
    payload: str, model: LLMModelConfig, *, max_output_tokens: int = 8192
) -> float:
    """A worst-case USD bound for one call to *model* with *payload* as the
    input.

    ``input_tokens = ceil(len(payload) / CHARS_PER_TOKEN)`` (a char-proxy estimate — no live
    provider token-counting call); output is priced at the full
    *max_output_tokens* ceiling, never an average actual. That ceiling already
    bounds hidden reasoning: Gemini bills thinking tokens as output and counts
    them against ``max_output_tokens``, so a thinking model cannot out-spend it. Callers pricing an
    LLM narrator call should pass ``narrate.llm.MAX_OUTPUT_TOKENS`` explicitly
    rather than relying on this default matching it by coincidence.

    Raises :class:`~commishdesk.errors.CostCeilingExceededError` when *model*'s
    ``"<provider>:<model_id>"`` key has no :data:`MODEL_PRICES` entry — fail
    closed on an unpriced model rather than silently skipping the estimate.
    """
    key = f"{model.provider}:{model.model_id}"
    price = MODEL_PRICES.get(key)
    if price is None:
        raise CostCeilingExceededError(
            f"no pricing entry for model {key!r} in narrate/pricing.py's "
            "MODEL_PRICES; add one before using this model, or the cost "
            "estimate cannot fail closed by name"
        )
    input_tokens = math.ceil(len(payload) / CHARS_PER_TOKEN)
    return (
        (input_tokens / 1000) * price.input_usd_per_1k
        + (max_output_tokens / 1000) * price.output_usd_per_1k
    )
