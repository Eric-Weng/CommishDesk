"""Stage 4 (opt-in) — the LLM narrator: one paid generation call per league-week.

This module is the *only* place the engine talks to a model provider. It:

* defines the :class:`LLMClient` protocol — ``generate(payload, voice) -> str`` —
  and two direct adapters, :class:`AnthropicClient` and :class:`GoogleClient`.
  There is **no aggregator / proxy** (``openai`` / ``litellm`` / ``openrouter``):
  the only provider SDKs it names are ``anthropic`` and ``google.genai``
  (AD-15), and both imports are **method-local** so the SDKs stay an optional
  dependency — ``import commishdesk.narrate`` pulls in neither them nor
  ``httpx`` and the zero-credential core (I4) is untouched;
* builds the model payload from the sanitized ``narration`` projection alone
  (:func:`build_narration_payload` — AD-1); it never sees a ``LeagueModel``,
  ``BoardMetrics``, a raw roster, or a box score;
* runs the fixed selection order ``primary -> fallback -> template``
  (:func:`narrate_draft_recap`): the primary is tried once, the fallback only on
  a primary failure, for **at most one successful** generation call per
  invocation regardless of member count (AD-8 / I3). All generation flows
  through the single ``client.generate(...)`` call site in
  :func:`narrate_with_llm`.

Model ids and endpoints are **not** here — they live in
:mod:`commishdesk.llmconfig`. Changing the config changes no file under
``narrate/``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, assert_never, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from commishdesk.errors import NarratorError
from commishdesk.llmconfig import LLMConfig, LLMModelConfig
from commishdesk.narrate.template import recap_to_text, render_draft_recap
from commishdesk.voices import Voice

if TYPE_CHECKING:
    from commishdesk.facts.schema import Narration

__all__ = [
    "AnthropicClient",
    "GoogleClient",
    "LLMClient",
    "NarrationResult",
    "build_client",
    "build_narration_payload",
    "narrate_draft_recap",
    "narrate_with_llm",
]

_LOGGER = logging.getLogger("commishdesk")

#: Output ceiling for one narration. The ``narration`` projection is held under
#: ``facts.schema.NARRATION_TOKEN_CAP`` (~5k tokens) upstream; the recap it
#: produces is comfortably smaller, so this is a generous hard stop, not a budget.
_MAX_OUTPUT_TOKENS = 8192

_LLMNarrator = Literal["llm-primary", "llm-fallback"]


# --------------------------------------------------------------------------- #
# The protocol + result model
# --------------------------------------------------------------------------- #


@runtime_checkable
class LLMClient(Protocol):
    """A single-shot text generator. One call == one paid generation (I3)."""

    def generate(self, payload: str, voice: Voice) -> str: ...


class NarrationResult(BaseModel):
    """What :func:`narrate_draft_recap` returns — narrated text plus its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    narrator: _LLMNarrator | Literal["template"]


# --------------------------------------------------------------------------- #
# Payload builder — the narration projection, nothing else (AD-1)
# --------------------------------------------------------------------------- #


def build_narration_payload(narration: Narration) -> str:
    """Serialize the ``narration`` projection to JSON for the model.

    ``Narration.model_dump_json()`` is deterministic (Pydantic v2 field order) and
    carries only the projection — no ``LeagueModel``, no raw board column.
    """
    return narration.model_dump_json()


# --------------------------------------------------------------------------- #
# Provider adapters — direct SDKs only, lazily imported (AD-15)
# --------------------------------------------------------------------------- #


def _env_key(*names: str, env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    for name in names:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class AnthropicClient:
    """Direct ``anthropic`` adapter. Key: ``ANTHROPIC_API_KEY`` or ``LLM_API_KEY``.

    *env* defaults to :data:`os.environ`; pass an explicit mapping to keep the
    real process environment out of a test (``llmconfig`` precedent).
    """

    model_id: str
    endpoint: str | None = None
    env: Mapping[str, str] | None = None

    def generate(self, payload: str, voice: Voice) -> str:
        try:
            import anthropic
        except ImportError as exc:  # SDK absent -> this provider is unusable
            raise NarratorError(
                "the 'anthropic' SDK is not installed; install commishdesk[llm]"
            ) from exc

        api_key = _env_key("ANTHROPIC_API_KEY", "LLM_API_KEY", env=self.env)
        if api_key is None:
            raise NarratorError(
                "no Anthropic API key (set ANTHROPIC_API_KEY or LLM_API_KEY)"
            )

        kwargs: dict[str, object] = {"api_key": api_key}
        if self.endpoint is not None:
            kwargs["base_url"] = self.endpoint
        client = anthropic.Anthropic(**kwargs)

        message = client.messages.create(
            model=self.model_id,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=voice.system_prompt,
            messages=[{"role": "user", "content": payload}],
        )
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise NarratorError("Anthropic completion was truncated (max_tokens)")
        parts = [
            getattr(block, "text", "")
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        text = "".join(parts).strip()
        if not text:
            raise NarratorError("Anthropic returned an empty completion")
        return text


@dataclass(frozen=True, slots=True)
class GoogleClient:
    """Direct ``google.genai`` adapter. Key: ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``.

    *env* defaults to :data:`os.environ`; pass an explicit mapping to keep the
    real process environment out of a test (``llmconfig`` precedent).
    """

    model_id: str
    endpoint: str | None = None
    env: Mapping[str, str] | None = None

    def generate(self, payload: str, voice: Voice) -> str:
        try:
            from google import genai
        except ImportError as exc:  # SDK absent -> this provider is unusable
            raise NarratorError(
                "the 'google-genai' SDK is not installed; install commishdesk[llm]"
            ) from exc

        api_key = _env_key("GEMINI_API_KEY", "GOOGLE_API_KEY", env=self.env)
        if api_key is None:
            raise NarratorError(
                "no Google API key (set GEMINI_API_KEY or GOOGLE_API_KEY)"
            )

        kwargs: dict[str, object] = {"api_key": api_key}
        if self.endpoint is not None:
            kwargs["http_options"] = {"base_url": self.endpoint}
        client = genai.Client(**kwargs)

        response = client.models.generate_content(
            model=self.model_id,
            contents=payload,
            config={
                "system_instruction": voice.system_prompt,
                "max_output_tokens": _MAX_OUTPUT_TOKENS,
            },
        )
        finish = _google_finish_reason(response)
        if finish is not None and finish != "STOP":
            raise NarratorError(f"Google completion did not finish cleanly ({finish})")
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise NarratorError("Google returned an empty completion")
        return text


def _google_finish_reason(response: object) -> str | None:
    """The first candidate's ``finish_reason`` as a plain string, or ``None`` when
    the response carries no candidate / no reason (``STOP`` == a clean finish;
    ``MAX_TOKENS`` / ``SAFETY`` / ``RECITATION`` == a failed attempt)."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    return getattr(reason, "name", str(reason))


def build_client(cfg: LLMModelConfig) -> LLMClient:
    """The adapter for *cfg*'s provider. ``llmconfig`` already validated the token."""
    if cfg.provider == "anthropic":
        return AnthropicClient(model_id=cfg.model_id, endpoint=cfg.endpoint)
    if cfg.provider == "google":
        return GoogleClient(model_id=cfg.model_id, endpoint=cfg.endpoint)
    assert_never(cfg.provider)


# --------------------------------------------------------------------------- #
# Selection — primary -> fallback -> template
# --------------------------------------------------------------------------- #


def narrate_with_llm(
    narration: Narration,
    voice: Voice,
    config: LLMConfig,
    *,
    client_factory: Callable[[LLMModelConfig], LLMClient] = build_client,
) -> tuple[str, _LLMNarrator]:
    """Try the primary once, then the fallback once. Return ``(text, tag)``.

    ``tag`` is ``"llm-primary"`` or ``"llm-fallback"``. At most two generation
    attempts total, all through the one ``client.generate(...)`` call site below.
    A provider that raises, returns a non-``str``, or returns an empty/whitespace
    completion counts as a failed attempt. Raises
    :class:`~commishdesk.errors.NarratorError` (chained from the last underlying
    exception) only when *both* attempts fail.
    """
    try:
        payload = build_narration_payload(narration)
    except Exception as exc:  # a malformed projection is a narrator fault, not a crash
        raise NarratorError("could not serialize the narration payload") from exc

    attempts: tuple[tuple[_LLMNarrator, LLMModelConfig], ...] = (
        ("llm-primary", config.primary),
        ("llm-fallback", config.fallback),
    )
    last_exc: Exception | None = None
    for tag, model_cfg in attempts:
        label = f"{tag} ({model_cfg.provider}:{model_cfg.model_id})"
        try:
            client = client_factory(model_cfg)
            text = client.generate(payload, voice)
        except Exception as exc:  # one call site: record the fault, then fall through
            last_exc = exc
            _LOGGER.warning("llm narrator %s failed: %s", label, exc)
            continue
        if not isinstance(text, str) or not text.strip():
            last_exc = NarratorError(
                f"{label} returned an unusable completion ({type(text).__name__})"
            )
            _LOGGER.warning("llm narrator %s returned an unusable completion", label)
            continue
        return text, tag
    raise NarratorError("both LLM providers failed to generate narration") from last_exc


def narrate_draft_recap(
    narration: Narration,
    voice: Voice,
    config: LLMConfig,
    *,
    llm_enabled: bool,
    client_factory: Callable[[LLMModelConfig], LLMClient] = build_client,
) -> NarrationResult:
    """The "never fails to produce narrated text" entry point (AC3).

    ``llm_enabled=False`` -> the template narrator's text, ``narrator="template"``,
    no client constructed and no SDK imported. ``llm_enabled=True`` -> the
    ``primary -> fallback -> template`` selection: a provider or generation fault
    is swallowed and logged, never propagated to the caller. A malformed
    ``COMMISHDESK_LLM_*`` value is a different failure mode — it raises from
    :func:`commishdesk.llmconfig.load_llm_config` at startup, by design, before
    this function ever runs.
    """
    if llm_enabled:
        try:
            text, tag = narrate_with_llm(
                narration, voice, config, client_factory=client_factory
            )
        except (NarratorError, ValidationError) as exc:
            _LOGGER.warning("llm narrator unavailable; using template narrator: %s", exc)
        else:
            return NarrationResult(text=text, narrator=tag)

    return NarrationResult(
        text=recap_to_text(render_draft_recap(narration)), narrator="template"
    )
