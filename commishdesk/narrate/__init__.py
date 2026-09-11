"""Stage 4 — turn the Facts JSON into prose via the zero-credential template
narrator (the floor) and the opt-in LLM narrator.

The template narrator (:func:`render_draft_recap` / :func:`recap_to_text`) is
deterministic and credential-free — it reads only the ``narration`` projection of
the Facts JSON (AD-1) and is imported eagerly. The LLM narrator lives in
``narrate/llm.py`` and its public names (:class:`LLMClient`,
:class:`NarrationResult`, :func:`build_narration_payload`,
:func:`narrate_with_llm`, :func:`narrate_draft_recap`) are re-exported **lazily**
via ``__getattr__`` — the same pattern as ``commishdesk.facts`` — so
``import commishdesk.narrate`` never pulls in ``anthropic`` / ``google.genai`` /
``httpx`` and the zero-credential core (I4) is protected. The one exception is
:data:`~commishdesk.narrate.llm.MAX_OUTPUT_TOKENS` (Story 4.6): a plain ``int``
constant with no SDK dependency of its own — the module-level imports in
``llm.py`` never touch a provider SDK either — so it is re-exported **eagerly**,
letting ``narrate/pricing.py``'s cost estimate (and ``cli.py``'s call site)
import the one real ceiling instead of a second, duplicable literal.

The deterministic content-safety check (``narrate/safety.py`` — AD-12 Layer 2:
:class:`SafetyFinding`, :class:`SafetyReport`, :func:`check_narration`) is
credential-free and imported **eagerly**, like ``template.py``.

The pre-call cost estimator (``narrate/pricing.py`` — Story 4.6:
:class:`ModelPrice`, :data:`MODEL_PRICES`, :data:`PRICING_UPDATED`,
:data:`PRICING_REVIEW_INTERVAL_DAYS`, :func:`is_pricing_stale`,
:func:`estimate_cost_usd`) has no SDK dependency either, so it is also imported
**eagerly**, like ``safety.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .llm import MAX_OUTPUT_TOKENS
from .pricing import (
    MODEL_PRICES,
    PRICING_REVIEW_INTERVAL_DAYS,
    PRICING_UPDATED,
    ModelPrice,
    estimate_cost_usd,
    is_pricing_stale,
)
from .response import (
    SECTION_HEADINGS,
    TieredResponse,
    classify,
    sanitize_completion,
    structural_ok,
    suppress_sections,
)
from .safety import (
    SafetyFinding,
    SafetyReport,
    check_narration,
    closed_world_tokens,
)
from .template import Recap, Section, recap_to_text, render_draft_recap

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "MODEL_PRICES",
    "PRICING_REVIEW_INTERVAL_DAYS",
    "PRICING_UPDATED",
    "SECTION_HEADINGS",
    "LLMClient",
    "ModelPrice",
    "NarrationResult",
    "Recap",
    "SafetyFinding",
    "SafetyReport",
    "Section",
    "TieredResponse",
    "build_narration_payload",
    "check_narration",
    "classify",
    "closed_world_tokens",
    "estimate_cost_usd",
    "is_pricing_stale",
    "narrate_draft_recap",
    "narrate_with_llm",
    "recap_to_text",
    "render_draft_recap",
    "sanitize_completion",
    "structural_ok",
    "suppress_sections",
]

_LAZY = {
    "LLMClient": ("commishdesk.narrate.llm", "LLMClient"),
    "NarrationResult": ("commishdesk.narrate.llm", "NarrationResult"),
    "build_narration_payload": ("commishdesk.narrate.llm", "build_narration_payload"),
    "narrate_draft_recap": ("commishdesk.narrate.llm", "narrate_draft_recap"),
    "narrate_with_llm": ("commishdesk.narrate.llm", "narrate_with_llm"),
}

if TYPE_CHECKING:
    from commishdesk.narrate.llm import (
        LLMClient,
        NarrationResult,
        build_narration_payload,
        narrate_draft_recap,
        narrate_with_llm,
    )


def __getattr__(name: str) -> object:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attr = target
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return sorted(__all__)
