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
  (:func:`build_narration_payload` / :func:`build_weekly_payload` — AD-1); it
  never sees a ``LeagueModel``, ``BoardMetrics``, a raw roster, or a box score;
* runs the fixed selection order ``primary -> fallback -> template``
  (:func:`narrate_draft_recap`, and Story 5.12's
  :func:`narrate_weekly_issue`): each provider is tried, on a *transient* fault
  (request/connect timeout, transport error, HTTP 429 / 5xx) retried on the same
  provider up to :data:`RETRY_CAP` more times — immediately, no backoff — then
  the fallback, then the template. A non-transient fault (missing SDK, missing
  key, truncated / empty completion, other 4xx) falls through on the first
  failure. Retrying a *failed* call spends nothing and yields no extra
  *successful* generation, so there is still **at most one successful**
  generation call per invocation regardless of member count (AD-8 / I3). All
  generation flows through the single ``client.generate(...)`` call site in
  :func:`_generate_first_success`, which owns the retry loop and stays SDK-free
  (the content-safety claim verifier, :func:`extract_with_llm`, bills through
  that same site);
  transient classification lives in the provider adapters.
* disables each SDK's own retry loop (``max_retries=0`` / ``retries: 0``) so
  this module's :data:`RETRY_CAP` is the *only* retry policy in play —
  otherwise a 429 would be multiplied by the SDK's own default before the
  engine's cap ever saw it (epic-3-retro-item-34).

Model ids, endpoints, and the per-attempt request timeout
(``COMMISHDESK_LLM_TIMEOUT``, default 60s) are **not** here — they live in
:mod:`commishdesk.llmconfig`. Changing the config changes no file under
``narrate/``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, assert_never, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from commishdesk.errors import NarratorError
from commishdesk.llmconfig import LLMConfig, LLMModelConfig
from commishdesk.narrate.template import recap_to_text, render_draft_recap
from commishdesk.narrate.weekly_template import (
    cold_start_standings,
    render_weekly_issue,
    weekly_issue_to_text,
)
from commishdesk.voices import Voice

if TYPE_CHECKING:
    from commishdesk.facts.schema import Narration, WeeklyNarration

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "AnthropicClient",
    "CallUsage",
    "GoogleClient",
    "LLMClient",
    "NarrationResult",
    "build_client",
    "build_narration_payload",
    "build_weekly_payload",
    "extract_with_llm",
    "narrate_draft_recap",
    "narrate_weekly_issue",
    "narrate_weekly_with_llm",
    "narrate_with_llm",
    "recording_usage",
]

_LOGGER = logging.getLogger("commishdesk")

#: Output ceiling for one narration. The ``narration`` projection is held under
#: ``facts.schema.NARRATION_TOKEN_CAP`` (~5k tokens) upstream; the recap it
#: produces is comfortably smaller, so this is a generous hard stop, not a budget.
#: Public (Story 4.6): ``narrate/pricing.py``'s worst-case cost estimate must
#: price the same ceiling the provider adapters below actually send, so it is
#: imported — never duplicated as a second literal — at the ``cli.py`` call
#: site, via ``narrate/__init__.py``'s eager re-export.
MAX_OUTPUT_TOKENS = 8192

#: How many *extra* attempts a transient provider fault buys on the *same*
#: provider before the selector falls through to the next one (FR-40 / Story
#: 3.6). Each provider is therefore attempted at most ``1 + RETRY_CAP`` times.
#: Retries are immediate — no backoff, no sleep, no clock dependency: the
#: independent fallback provider (AD-15) is the resilience mechanism for a
#: sustained outage; this only absorbs a momentary blip. A retried *failed* call
#: spends nothing and yields no extra *successful* generation, so I3 holds.
RETRY_CAP = 2

#: Both SDKs ship their own retry loop, which this engine deliberately turns
#: off. Left on, one throttled request would fan out into the SDK's own
#: ``max_retries`` attempts *per* engine attempt — up to ``(1 + SDK) * (1 +
#: RETRY_CAP)`` billed calls where the engine's own budget thought it was
#: spending ``1 + RETRY_CAP`` (epic-3-retro-item-34).
_SDK_MAX_RETRIES = 0

_LLMNarrator = Literal["llm-primary", "llm-fallback"]


class _TransientProviderError(NarratorError):
    """A retryable provider fault — a request/connect timeout, a transport error,
    or an HTTP 429 / 5xx. Raised by a provider adapter (where the SDK is
    imported) and caught by :func:`narrate_with_llm`'s retry loop. Every other
    fault is a plain :class:`~commishdesk.errors.NarratorError` (or propagates)
    and is *not* retried. Module-private: not exported, not a public signal."""


# --------------------------------------------------------------------------- #
# The protocol + result model
# --------------------------------------------------------------------------- #


@runtime_checkable
class LLMClient(Protocol):
    """A single-shot text generator. One call == one paid generation (I3)."""

    def generate(self, payload: str, voice: Voice) -> str: ...


class NarrationResult(BaseModel):
    """What :func:`narrate_draft_recap` / :func:`narrate_weekly_issue` return —
    narrated text plus its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    narrator: _LLMNarrator | Literal["template"]


# --------------------------------------------------------------------------- #
# Payload builders — the narration projection, nothing else (AD-1)
# --------------------------------------------------------------------------- #


def build_narration_payload(narration: Narration) -> str:
    """Serialize the draft-recap ``narration`` projection to JSON for the model.

    ``Narration.model_dump_json()`` is deterministic (Pydantic v2 field order) and
    carries only the projection — no ``LeagueModel``, no raw board column.
    """
    return narration.model_dump_json()


def build_weekly_payload(narration: WeeklyNarration, *, cold_start: bool = False) -> str:
    """Serialize the weekly ``narration`` projection to JSON for the model.

    Same contract as :func:`build_narration_payload`, for the standalone weekly
    Issue (Story 5.12). ``cli.py`` calls this too, so the payload the pre-call
    cost estimate is priced against is *byte-identical* to the one
    :func:`narrate_weekly_issue` actually sends.

    Story 5.16: ``cold_start=True`` omits ``playoff_picture``, ``transactions``,
    ``power``, ``luck`` and ``storyline_candidates`` from the model payload and
    orders ``standings`` by points with positional ranks. Warm payload bytes stay
    unchanged.
    """
    if not cold_start:
        return narration.model_dump_json()
    cold = narration.model_copy(update={"standings": cold_start_standings(narration.standings)})
    return cold.model_dump_json(
        exclude={"playoff_picture", "transactions", "power", "luck", "storyline_candidates"}
    )


# --------------------------------------------------------------------------- #
# Provider adapters — direct SDKs only, lazily imported (AD-15)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Usage — what each call actually billed, measured rather than estimated
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CallUsage:
    """The token usage one provider call reported, as the provider bills it.

    Recorded for every completion that comes back — including one then rejected
    as truncated, because a truncated completion is still billed.
    ``thinking_tokens`` is reasoning the model never returned but still billed as
    output (Gemini's ``thoughts_token_count``). ``None`` means the provider
    reported no figure, never zero.

    It exists because estimates were not enough. On 2026-09-13 a day of live
    testing was estimated from character counts at ~1.07M input / ~0.55M output
    tokens; the provider billed 1.5M / 1.0M. Dense JSON tokenizes nearer 3
    characters a token than 4, and hidden thinking roughly doubled the output.
    """

    provider: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None = None

    @property
    def billed_output_tokens(self) -> int | None:
        """Visible output plus thinking — everything billed at the output rate."""
        if self.output_tokens is None and self.thinking_tokens is None:
            return None
        return (self.output_tokens or 0) + (self.thinking_tokens or 0)

    def usd(self) -> float | None:
        """List-price cost from ``narrate/pricing.py``, or ``None`` when the model
        is unpriced or the provider reported no token counts."""
        from commishdesk.narrate.pricing import MODEL_PRICES

        price = MODEL_PRICES.get(f"{self.provider}:{self.model_id}")
        output = self.billed_output_tokens
        if price is None or self.input_tokens is None or output is None:
            return None
        return self.input_tokens / 1000 * price.input_usd_per_1k + output / 1000 * price.output_usd_per_1k


_USAGE: ContextVar[list[CallUsage] | None] = ContextVar("commishdesk_llm_usage", default=None)


@contextmanager
def recording_usage() -> Iterator[list[CallUsage]]:
    """Collect every :class:`CallUsage` recorded inside the ``with`` block.

    Context-local, so concurrent runs on separate threads never mix figures, and
    a nested block collects only its own calls. Outside any block, usage is still
    logged — it is just not collected.
    """
    bucket: list[CallUsage] = []
    token = _USAGE.set(bucket)
    try:
        yield bucket
    finally:
        _USAGE.reset(token)


def _record_usage(usage: CallUsage) -> None:
    usd = usage.usd()
    _LOGGER.info(
        "llm usage %s:%s input=%s output=%s thinking=%s usd=%s",
        usage.provider,
        usage.model_id,
        usage.input_tokens,
        usage.output_tokens,
        usage.thinking_tokens,
        "unknown" if usd is None else f"{usd:.5f}",
    )
    bucket = _USAGE.get()
    if bucket is not None:
        bucket.append(usage)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _env_key(*names: str, env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    for name in names:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _is_transport_fault(exc: BaseException) -> bool:
    """True for a raw transport-layer fault (connect / read / write / timeout)
    from either SDK's underlying HTTP client. ``httpx`` is a hard dependency, but
    the import stays local so ``import commishdesk.narrate`` still pulls in
    nothing (I4 / the no-``httpx``-on-import guard)."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return False
    return isinstance(exc, httpx.TransportError)


#: Phrases that mark a provider error as an exhausted account rather than a
#: rate limit. A rate limit clears on its own and earns an immediate retry;
#: depleted prepaid credits or a billing block do not, and retrying only repeats
#: a rejected request (live, 2026-09-13: every verifier call was tried three
#: times against "Your prepayment credits are depleted"). Deliberately narrow —
#: a bare "quota" also appears in Google's ordinary per-minute rate-limit message.
_BILLING_EXHAUSTED_MARKERS = ("prepayment", "credit", "billing")


def _is_billing_exhaustion(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _BILLING_EXHAUSTED_MARKERS)


def _raise_if_transient_anthropic(exc: Exception, anthropic: object) -> None:
    """Re-raise *exc* as :class:`_TransientProviderError` when it is a retryable
    Anthropic fault (request/connect timeout, transport error, HTTP 429, HTTP
    >= 500); return silently otherwise so the caller re-raises it unchanged
    (non-transient — falling through is always safe)."""
    if _is_billing_exhaustion(exc):
        return  # an exhausted account, not a transient fault: never retried
    transient_types = tuple(
        t
        for t in (
            getattr(anthropic, "APITimeoutError", None),
            getattr(anthropic, "APIConnectionError", None),
            getattr(anthropic, "RateLimitError", None),
        )
        if isinstance(t, type)
    )
    if transient_types and isinstance(exc, transient_types):
        raise _TransientProviderError(f"Anthropic transient fault: {exc!r}") from exc

    status_error = getattr(anthropic, "APIStatusError", None)
    if isinstance(status_error, type) and isinstance(exc, status_error):
        code = getattr(exc, "status_code", None)
        if code in (408, 429) or (isinstance(code, int) and 500 <= code < 600):
            raise _TransientProviderError(f"Anthropic HTTP {code}: {exc!r}") from exc

    if _is_transport_fault(exc):
        raise _TransientProviderError(f"Anthropic transport fault: {exc!r}") from exc


def _raise_if_transient_google(exc: Exception, errors: object) -> None:
    """Google-genai counterpart of :func:`_raise_if_transient_anthropic`.

    *errors* is ``google.genai.errors`` (or ``None`` when it cannot be imported —
    then only the raw transport check applies)."""
    if _is_billing_exhaustion(exc):
        return  # depleted credits / billing block, not a rate limit: never retried
    if errors is not None:
        server_error = getattr(errors, "ServerError", None)
        if isinstance(server_error, type) and isinstance(exc, server_error):
            raise _TransientProviderError(f"Google server error: {exc!r}") from exc

        api_error = getattr(errors, "APIError", None)
        if isinstance(api_error, type) and isinstance(exc, api_error):
            code = getattr(exc, "code", None)
            if code in (408, 429) or (isinstance(code, int) and 500 <= code < 600):
                raise _TransientProviderError(f"Google HTTP {code}: {exc!r}") from exc

    if _is_transport_fault(exc):
        raise _TransientProviderError(f"Google transport fault: {exc!r}") from exc


@dataclass(frozen=True, slots=True)
class AnthropicClient:
    """Direct ``anthropic`` adapter. Key: ``ANTHROPIC_API_KEY`` or ``LLM_API_KEY``.

    *env* defaults to :data:`os.environ`; pass an explicit mapping to keep the
    real process environment out of a test (``llmconfig`` precedent).
    """

    model_id: str
    endpoint: str | None = None
    env: Mapping[str, str] | None = None
    #: Per-attempt request timeout in seconds (``COMMISHDESK_LLM_TIMEOUT``);
    #: ``None`` leaves the SDK default alone.
    timeout: float | None = None

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

        kwargs: dict[str, object] = {"api_key": api_key, "max_retries": _SDK_MAX_RETRIES}
        if self.endpoint is not None:
            kwargs["base_url"] = self.endpoint
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        client = anthropic.Anthropic(**kwargs)

        try:
            message = client.messages.create(
                model=self.model_id,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=voice.system_prompt,
                messages=[{"role": "user", "content": payload}],
            )
        except Exception as exc:  # a transient fault is retryable; the rest falls through
            _raise_if_transient_anthropic(exc, anthropic)
            raise
        usage = getattr(message, "usage", None)
        _record_usage(
            CallUsage(
                provider="anthropic",
                model_id=self.model_id,
                input_tokens=_int_or_none(getattr(usage, "input_tokens", None)),
                output_tokens=_int_or_none(getattr(usage, "output_tokens", None)),
            )
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
    #: Per-attempt request timeout in seconds (``COMMISHDESK_LLM_TIMEOUT``);
    #: ``None`` leaves the SDK default alone. google-genai wants milliseconds.
    timeout: float | None = None

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

        # ``retries`` is always present: the engine owns the retry policy
        # (RETRY_CAP) and the SDK's own loop is turned off underneath it.
        http_options: dict[str, object] = {"retries": _SDK_MAX_RETRIES}
        if self.endpoint is not None:
            http_options["base_url"] = self.endpoint
        if self.timeout is not None:
            # seconds -> ms, floored at 1: a sub-ms timeout must not become 0,
            # which google-genai reads as "no timeout".
            http_options["timeout"] = max(1, round(self.timeout * 1000))
        kwargs: dict[str, object] = {"api_key": api_key, "http_options": http_options}
        client = genai.Client(**kwargs)

        try:
            response = client.models.generate_content(
                model=self.model_id,
                contents=payload,
                config={
                    "system_instruction": voice.system_prompt,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                    # Gemini 3.x's default extended-thinking budget is billed
                    # against max_output_tokens right alongside the visible
                    # completion. A narration call needs no chain-of-thought,
                    # and left at its default the thinking budget can consume
                    # nearly all of MAX_OUTPUT_TOKENS on a real narration
                    # payload — confirmed live against the configured fallback
                    # model: thinking spent 7860 of the 8192-token ceiling,
                    # leaving 328 for visible text and truncating at
                    # MAX_TOKENS before any usable prose. Zeroing it here both
                    # fixes that (finish_reason -> STOP, full completion) and
                    # roughly halves the billed tokens for the same call
                    # (11,751 -> 6,189 total, measured).
                    "thinking_config": {"thinking_budget": 0},
                },
            )
        except Exception as exc:  # a transient fault is retryable; the rest falls through
            try:
                from google.genai import errors as genai_errors
            except ImportError:
                genai_errors = None  # type: ignore[assignment]
            _raise_if_transient_google(exc, genai_errors)
            raise
        meta = getattr(response, "usage_metadata", None)
        _record_usage(
            CallUsage(
                provider="google",
                model_id=self.model_id,
                input_tokens=_int_or_none(getattr(meta, "prompt_token_count", None)),
                output_tokens=_int_or_none(getattr(meta, "candidates_token_count", None)),
                thinking_tokens=_int_or_none(getattr(meta, "thoughts_token_count", None)),
            )
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
        return AnthropicClient(
            model_id=cfg.model_id, endpoint=cfg.endpoint, timeout=cfg.timeout
        )
    if cfg.provider == "google":
        return GoogleClient(
            model_id=cfg.model_id, endpoint=cfg.endpoint, timeout=cfg.timeout
        )
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
    """Try the primary, then the fallback. Return ``(text, tag)``.

    ``tag`` is ``"llm-primary"`` or ``"llm-fallback"``. Each provider is attempted
    at most ``1 + RETRY_CAP`` times: a :class:`_TransientProviderError` (timeout /
    transport / 429 / 5xx) retries the *same* provider immediately (no backoff);
    any other exception, or a non-``str`` / empty / whitespace completion, is a
    non-transient failure that falls straight through to the next provider —
    today's behaviour, unchanged. Every attempt runs through the one
    ``client.generate(...)`` call site in :func:`_generate_first_success`. A retried *failed* call yields no
    extra *successful* generation, so I3 (one paid call per league-week) holds.
    Raises :class:`~commishdesk.errors.NarratorError` (chained from the last
    underlying exception) only when *every* attempt fails.
    """
    try:
        payload = build_narration_payload(narration)
    except Exception as exc:  # a malformed projection is a narrator fault, not a crash
        raise NarratorError("could not serialize the narration payload") from exc

    attempts: tuple[tuple[_LLMNarrator, LLMModelConfig], ...] = (
        ("llm-primary", config.primary),
        ("llm-fallback", config.fallback),
    )
    return _generate_first_success(
        payload,
        voice,
        attempts,
        client_factory=client_factory,
        role="narrator",
        failure_message="both LLM providers failed to generate narration",
    )


def narrate_weekly_with_llm(
    narration: WeeklyNarration,
    voice: Voice,
    config: LLMConfig,
    *,
    client_factory: Callable[[LLMModelConfig], LLMClient] = build_client,
    cold_start: bool = False,
) -> tuple[str, _LLMNarrator]:
    """The weekly counterpart of :func:`narrate_with_llm` (Story 5.12).

    Identical selection, retry and failure semantics — the only differences are
    the payload (:func:`build_weekly_payload`) and the log role. It routes
    through the *same* :func:`_generate_first_success` call site, so the package
    still has exactly one ``.generate(`` site and I3's structural guard holds for
    the weekly path too.
    """
    try:
        payload = build_weekly_payload(narration, cold_start=cold_start)
    except Exception as exc:  # a malformed projection is a narrator fault, not a crash
        raise NarratorError("could not serialize the weekly narration payload") from exc

    attempts: tuple[tuple[_LLMNarrator, LLMModelConfig], ...] = (
        ("llm-primary", config.primary),
        ("llm-fallback", config.fallback),
    )
    return _generate_first_success(
        payload,
        voice,
        attempts,
        client_factory=client_factory,
        role="weekly narrator",
        failure_message="both LLM providers failed to generate the weekly narration",
    )


def _generate_first_success[Tag: str](
    payload: str,
    voice: Voice,
    attempts: Sequence[tuple[Tag, LLMModelConfig]],
    *,
    client_factory: Callable[[LLMModelConfig], LLMClient],
    role: str,
    failure_message: str,
) -> tuple[str, Tag]:
    """Try each ``(tag, model)`` in order and return ``(text, tag)`` for the first
    usable completion — **the package's one and only ``generate`` call site**.

    Every paid call routes through here: narration (primary -> fallback), the
    weekly narration (Story 5.12), and, since content-safety P1, the claim
    verifier (a single provider). I3's structural guard counts ``.generate(``
    call sites across the whole package and allows exactly one, so a second
    billing path cannot appear without tripping it.

    Each provider gets ``1 + RETRY_CAP`` attempts: a :class:`_TransientProviderError`
    retries the same provider immediately; any other exception, or a non-``str``
    / empty completion, falls through to the next provider. *role* only labels
    the log lines — ``"narrator"`` reproduces the pre-P1 messages exactly.
    Raises :class:`~commishdesk.errors.NarratorError` (``failure_message``,
    chained from the last underlying exception) when every attempt fails.
    """
    prefix = f"llm {role}"
    last_exc: Exception | None = None
    for tag, model_cfg in attempts:
        label = f"{tag} ({model_cfg.provider}:{model_cfg.model_id})"
        try:
            client = client_factory(model_cfg)
        except Exception as exc:  # building the adapter failed -> next provider
            last_exc = exc
            _LOGGER.warning(prefix + " %s failed: %s", label, exc)
            continue
        for attempt in range(1 + RETRY_CAP):
            try:
                text = client.generate(payload, voice)  # the one call site
            except _TransientProviderError as exc:  # retry the same provider, no backoff
                last_exc = exc
                _LOGGER.warning(
                    prefix + " %s transient fault (attempt %d/%d): %s",
                    label,
                    attempt + 1,
                    1 + RETRY_CAP,
                    exc,
                )
                continue
            except Exception as exc:  # non-transient: record it, fall through
                last_exc = exc
                _LOGGER.warning(prefix + " %s failed: %s", label, exc)
                break
            if not isinstance(text, str) or not text.strip():
                last_exc = NarratorError(
                    f"{label} returned an unusable completion ({type(text).__name__})"
                )
                _LOGGER.warning(prefix + " %s returned an unusable completion", label)
                break
            return text, tag
    raise NarratorError(failure_message) from last_exc


def extract_with_llm(
    prose: str,
    voice: Voice,
    config: LLMModelConfig,
    *,
    client_factory: Callable[[LLMModelConfig], LLMClient] = build_client,
) -> str:
    """The content-safety claim-verification call (P1): one extraction over the
    finished prose, billed through :func:`_generate_first_success` — the same
    single call site and transient-retry policy as narration.

    One provider only, deliberately: there is no fallback, because
    ``narrate/verify.py`` fails *open* when this raises rather than buying a
    second provider's call. Raises :class:`~commishdesk.errors.NarratorError`
    when no usable completion comes back.
    """
    text, _tag = _generate_first_success(
        prose,
        voice,
        (("verifier", config),),
        client_factory=client_factory,
        role="verifier",
        failure_message="the claim-verification provider failed",
    )
    return text


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


def narrate_weekly_issue(
    narration: WeeklyNarration,
    voice: Voice,
    config: LLMConfig,
    *,
    llm_enabled: bool,
    client_factory: Callable[[LLMModelConfig], LLMClient] = build_client,
    cold_start: bool = False,
) -> NarrationResult:
    """The weekly counterpart of :func:`narrate_draft_recap` (Story 5.12).

    ``llm_enabled=False`` -> the deterministic weekly template Issue's text,
    ``narrator="template"``, no client constructed and no SDK imported.
    ``llm_enabled=True`` -> the ``primary -> fallback -> template`` selection,
    through the same single :func:`_generate_first_success` call site: a provider
    or generation fault is swallowed and logged, never propagated. The caller
    (``cli.py``) owns *whether* the weekly path may spend at all — this function
    only owns what happens once it may, exactly as ``narrate_draft_recap`` does.

    ``cold_start=True`` (Story 5.16) propagates to :func:`narrate_weekly_with_llm`
    so the payload omits the sections a Week-1 Issue does not carry.
    """
    if llm_enabled:
        try:
            text, tag = narrate_weekly_with_llm(
                narration,
                voice,
                config,
                client_factory=client_factory,
                cold_start=cold_start,
            )
        except (NarratorError, ValidationError) as exc:
            _LOGGER.warning("weekly llm narrator unavailable; using template narrator: %s", exc)
        else:
            return NarrationResult(text=text, narrator=tag)

    return NarrationResult(
        text=weekly_issue_to_text(render_weekly_issue(narration, has_prior_week=not cold_start)),
        narrator="template",
    )
