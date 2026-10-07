"""Story 4.6 — a dated, static price table plus a worst-case per-call cost
estimator, so ``commishdesk/cli.py`` can price a narration attempt before it
happens (FR-21's "one command ... tells me what it cost").

:func:`estimate_cost_usd` is a single-call estimator: it prices *one* call to
*one* model against *one* payload. The per-league sum ("how many billable
attempts can one league-week make", primary plus fallback, plus the verifier)
lives in :func:`narration_worst_case_usd`; the call-count constants
:data:`MAX_BILLABLE_NARRATION_ATTEMPTS` and :data:`MAX_BILLABLE_VERIFICATION_CALLS`
are defined here once, and ``cli.py`` and ``weekly.py`` import them.
:func:`league_worst_case_usd` is the payload-free form (AD-45, AD-46): the most one
league-week can cost at a ladder level, before any Facts exist.

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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from commishdesk.errors import CostCeilingExceededError
from commishdesk.llmconfig import LLMConfig, LLMModelConfig, load_llm_config

__all__ = [
    "BILLING_LEVELS",
    "CHARS_PER_TOKEN",
    "FREE_LEVELS",
    "MAX_BILLABLE_NARRATION_ATTEMPTS",
    "MAX_BILLABLE_VERIFICATION_CALLS",
    "MODEL_PRICES",
    "PRICING_REVIEW_INTERVAL_DAYS",
    "PRICING_UPDATED",
    "ModelPrice",
    "config_for_level",
    "estimate_cost_usd",
    "is_pricing_stale",
    "league_worst_case_usd",
    "narration_worst_case_usd",
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
    # Added 2026-09-14, direct from Google's pricing page: $0.25 / $1.50 per 1M
    # (output "including thinking tokens"), i.e. $0.00025 / $0.0015 per 1k. The
    # default claim verifier: on 20 shipped Issues with 40 planted errors it caught
    # 33 alone and 39 together with narrate/verify.py's free pattern claims
    # (gemini-3.8-flash alone: 38), at about a third of that model's cost.
    "google:gemini-3.1-flash-lite": ModelPrice(
        input_usd_per_1k=0.00025, output_usd_per_1k=0.0015
    ),
}

#: ISO date the table above was last checked against live provider pricing.
PRICING_UPDATED = "2026-09-14"

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


#: Per league-week, at most two narration *attempts* can each yield one
#: successful, fully-billed completion: the initial attempt, and the single
#: ``regenerate``-tier re-narration permitted (I3 reconciliation,
#: epic-3-retro-item-44 / sprint-status.yaml: "at most one successful generation
#: per narration attempt, at most two narration attempts per league-week").
#: ``RETRY_CAP``-driven transient retries never produce a billed completion, so
#: they need no multiplier here. The one definition (AD-46).
MAX_BILLABLE_NARRATION_ATTEMPTS = 2

#: Per league-week, at most one claim-verification call (content-safety P1).
#: ``_produce_issue`` verifies only the prose it is about to ship, and both of its
#: LLM ship paths return through that one check — so a regeneration never buys a
#: second verification, and member count never enters into it.
MAX_BILLABLE_VERIFICATION_CALLS = 1

#: Ladder levels (``policy/operator.toml``, AD-43) at which the engine bills.
BILLING_LEVELS = frozenset({"normal", "no_new_activations", "budget_model"})

#: Ladder levels with zero LLM calls.
FREE_LEVELS = frozenset({"template", "discord_only", "stop"})


def narration_worst_case_usd(
    payload: str,
    voice_prompt: str,
    config: LLMConfig,
    *,
    attempts: int | None = None,
    verifier_calls: int | None = None,
) -> float:
    """The worst-case USD for one league-week's paid calls: *payload* plus the
    voice's system prompt as input.

    Primary AND fallback both billed — a non-transient failure on the primary
    can fall through to the fallback within one narration attempt — times
    :data:`MAX_BILLABLE_NARRATION_ATTEMPTS`, plus the claim verifier times
    :data:`MAX_BILLABLE_VERIFICATION_CALLS` when ``config.verifier`` is set. An
    unpriced model raises :class:`~commishdesk.errors.CostCeilingExceededError`.
    *attempts* / *verifier_calls* default to the two constants; a caller that
    re-exports them under its own name (``cli.py``) passes its own so a test can
    patch them there.
    """
    from commishdesk.narrate.llm import MAX_OUTPUT_TOKENS

    n_attempts = MAX_BILLABLE_NARRATION_ATTEMPTS if attempts is None else attempts
    n_verifier = MAX_BILLABLE_VERIFICATION_CALLS if verifier_calls is None else verifier_calls

    priced = payload + voice_prompt
    per_call = estimate_cost_usd(
        priced, config.primary, max_output_tokens=MAX_OUTPUT_TOKENS
    ) + estimate_cost_usd(priced, config.fallback, max_output_tokens=MAX_OUTPUT_TOKENS)
    estimate = per_call * n_attempts
    if config.verifier is not None:
        from commishdesk.narrate.verify import EXTRACTOR_VOICE

        # The claim verifier reads the finished prose — at most one completion
        # long, so MAX_OUTPUT_TOKENS tokens at CHARS_PER_TOKEN — plus its own
        # system prompt; its reply is priced at the same output ceiling.
        verifier_input = (
            "x" * int(MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN) + EXTRACTOR_VOICE.system_prompt
        )
        estimate += (
            estimate_cost_usd(
                verifier_input, config.verifier, max_output_tokens=MAX_OUTPUT_TOKENS
            )
            * n_verifier
        )
    return estimate


def config_for_level(config: LLMConfig, level: str) -> LLMConfig | None:
    """The config the engine narrates with at ladder *level*, or ``None`` when
    the level makes no LLM call (``template`` and beyond).

    ``budget_model`` swaps the fallback in as the primary (AD-43) — a config
    change, so no model id is edited under ``narrate/``. Raises
    :class:`ValueError` for a level that is not on the ladder.
    """
    if level in FREE_LEVELS:
        return None
    if level == "budget_model":
        return replace(config, primary=config.fallback)
    if level in BILLING_LEVELS:
        return config
    raise ValueError(f"unknown ladder level {level!r}")


def league_worst_case_usd(level: str, config: LLMConfig | None = None) -> float:
    """The most one league-week can cost at ladder *level*, with no Facts at hand
    (AD-45, AD-46).

    Input is the largest payload the engine can send — ``NARRATION_TOKEN_CAP``
    characters (``facts/build.py`` refuses a larger one) plus the longest voice
    system prompt — priced at :data:`CHARS_PER_TOKEN`; output at
    ``MAX_OUTPUT_TOKENS``. ``template``, ``discord_only`` and ``stop`` return
    ``0.0``; an unknown level raises :class:`ValueError`. *config* defaults to
    ``load_llm_config()``.
    """
    if level not in BILLING_LEVELS | FREE_LEVELS:
        raise ValueError(f"unknown ladder level {level!r}")
    if level in FREE_LEVELS:
        return 0.0
    effective = config_for_level(config if config is not None else load_llm_config(), level)
    assert effective is not None  # billing levels always carry a config
    from commishdesk.facts.schema import NARRATION_TOKEN_CAP
    from commishdesk.voices.beat_writer import (
        BEAT_WRITER,
        BEAT_WRITER_WEEKLY,
        BEAT_WRITER_WEEKLY_COLD_START,
    )

    longest_prompt = max(
        (
            v.system_prompt
            for v in (BEAT_WRITER, BEAT_WRITER_WEEKLY, BEAT_WRITER_WEEKLY_COLD_START)
        ),
        key=len,
    )
    return narration_worst_case_usd("x" * NARRATION_TOKEN_CAP, longest_prompt, effective)
