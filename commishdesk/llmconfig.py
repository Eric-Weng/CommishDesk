"""Engine-level LLM model configuration — where a provider/model swap lives.

Standard library only (the ``store.py`` / ``logconfig.py`` precedent: engine-level
config, not per-league). :func:`load_llm_config` reads the process environment
(any ``Mapping[str, str]``, defaulting to :data:`os.environ`) and returns an
:class:`LLMConfig` — a ``primary`` and a ``fallback`` :class:`LLMModelConfig`.

No model-id or endpoint string literal appears anywhere under ``narrate/``:
swapping the primary or fallback model is a change here or an environment
variable, never a code change under ``narrate/``.

Environment variables (all optional; the defaults below apply when a variable is
unset or blank):

* ``COMMISHDESK_LLM_PRIMARY`` / ``COMMISHDESK_LLM_FALLBACK`` —
  ``"<provider>:<model_id>"``, where provider is ``anthropic`` or ``google``.
  Defaults: ``anthropic:claude-sonnet-5`` and ``google:gemini-3.5-flash``.
* ``COMMISHDESK_LLM_PRIMARY_ENDPOINT`` / ``COMMISHDESK_LLM_FALLBACK_ENDPOINT`` —
  an optional base-URL override for that provider's SDK; unset means the SDK
  default.
* ``COMMISHDESK_LLM_TIMEOUT`` — the per-attempt request timeout in seconds
  (float, default ``60.0``, must be ``> 0``, finite, and ``<= 600``). Applied by
  both provider adapters to their SDK client so a hung endpoint fails fast
  instead of stalling the one league-week narration for the SDK default
  (FR-40 / Story 3.6).
* ``COMMISHDESK_COST_CEILING_USD`` — the hard-abort ceiling (float, USD, default
  ``1.00``, must be ``> 0`` and finite) that ``commishdesk/cli.py`` checks a
  pre-call worst-case cost estimate against before any paid narration call
  (Story 4.6). Over the ceiling the run hard-aborts with zero spend.
* ``COMMISHDESK_LLM_VERIFIER`` — the content-safety claim verifier (P1),
  ``"<provider>:<model_id>"``. Default ``google:gemini-3.8-flash``, the cheapest
  priced model: the verifier only extracts claims, and the pre-call cost estimate
  fails closed on an unpriced model. ``off`` (or ``none`` / ``disabled`` /
  ``false`` / ``0`` / ``no``) switches verification off. A verifier whose provider
  has no API key does not abort anything — it fails open and the Issue ships on
  the deterministic checks.
* ``COMMISHDESK_LLM_VERIFIER_ENDPOINT`` — optional base-URL override for it.

An unknown provider token, a value that is not ``<provider>:<model_id>``, a
non-numeric / non-positive / over-600 ``COMMISHDESK_LLM_TIMEOUT``, or a
non-numeric / non-positive ``COMMISHDESK_COST_CEILING_USD`` raises
:class:`~commishdesk.errors.NarratorError` at load.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

from commishdesk.errors import NarratorError

__all__ = ["LLMConfig", "LLMModelConfig", "load_llm_config"]

Provider = Literal["anthropic", "google"]
_PROVIDERS: frozenset[str] = frozenset(get_args(Provider))

_DEFAULT_PRIMARY = "anthropic:claude-sonnet-5"
_DEFAULT_FALLBACK = "google:gemini-3.5-flash"

#: Values of ``COMMISHDESK_LLM_VERIFIER`` that switch claim verification off.
_VERIFIER_OFF: frozenset[str] = frozenset({"off", "none", "disabled", "false", "0", "no"})

#: Per-attempt request timeout (seconds) when ``COMMISHDESK_LLM_TIMEOUT`` is unset
#: or blank. A generous ceiling, not a budget — the point is that a hung endpoint
#: fails fast rather than stalling one league-week for the SDK default (FR-40).
_DEFAULT_TIMEOUT_SECONDS = 60.0

#: Upper bound on ``COMMISHDESK_LLM_TIMEOUT``. A larger per-attempt timeout, times
#: the retry cap, re-introduces the multi-hour stall FR-40 exists to prevent —
#: 10 minutes is already the order of magnitude of the SDK's own default.
_MAX_TIMEOUT_SECONDS = 600.0

#: Hard-abort cost ceiling (USD) when ``COMMISHDESK_COST_CEILING_USD`` is unset
#: or blank. A budget-smoke-alarm default, not a prediction of typical spend
#: (Story 4.6 / epic-4-context.md "Budget cap is a smoke alarm, not a daemon").
_DEFAULT_COST_CEILING_USD = 1.00


@dataclass(frozen=True, slots=True)
class LLMModelConfig:
    """One provider + model id, plus an optional base-URL override.

    ``timeout`` is the per-attempt request timeout in seconds that both provider
    adapters apply to their SDK client. :func:`load_llm_config` always sets it
    (default :data:`_DEFAULT_TIMEOUT_SECONDS`); it stays ``None`` only for a
    config hand-built in a test, where the adapter leaves the SDK default alone.
    """

    provider: Provider
    model_id: str
    endpoint: str | None = None
    timeout: float | None = None


#: The content-safety claim verifier when ``COMMISHDESK_LLM_VERIFIER`` is unset. It
#: only extracts claims, so it runs on the cheapest model in
#: ``narrate/pricing.py``'s table — which it has to be in, or the pre-call cost
#: estimate fails closed by name.
_DEFAULT_VERIFIER_CONFIG = LLMModelConfig(
    provider="google", model_id="gemini-3.8-flash", timeout=_DEFAULT_TIMEOUT_SECONDS
)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """The two models the selector tries, in order: ``primary`` then ``fallback``.

    ``cost_ceiling_usd`` (Story 4.6) is the hard-abort ceiling
    :func:`load_llm_config` always sets (default :data:`_DEFAULT_COST_CEILING_USD`);
    a hand-built config in a test that omits it gets that same default, so an
    older direct ``LLMConfig(primary=..., fallback=...)`` construction still
    compares equal to ``load_llm_config({})``.
    """

    primary: LLMModelConfig
    fallback: LLMModelConfig
    cost_ceiling_usd: float = _DEFAULT_COST_CEILING_USD
    #: The content-safety claim verifier (P1), or ``None`` when switched off. The
    #: dataclass default is exactly what :func:`load_llm_config` builds from an
    #: empty environment, so a hand-built ``LLMConfig(primary=..., fallback=...)``
    #: still compares equal to ``load_llm_config({})``.
    verifier: LLMModelConfig | None = _DEFAULT_VERIFIER_CONFIG


def _parse_model_spec(
    spec: str, *, var: str, endpoint: str | None, timeout: float
) -> LLMModelConfig:
    provider, sep, model_id = spec.partition(":")
    provider, model_id = provider.strip(), model_id.strip()
    if not sep or not provider or not model_id:
        raise NarratorError(f"{var} must be '<provider>:<model_id>', got {spec!r}")
    if provider not in _PROVIDERS:
        raise NarratorError(
            f"{var} names an unknown provider {provider!r}; "
            f"expected one of {sorted(_PROVIDERS)}"
        )
    return LLMModelConfig(
        provider=cast(Provider, provider),
        model_id=model_id,
        endpoint=endpoint,
        timeout=timeout,
    )


def _parse_timeout(raw: str | None) -> float:
    """``COMMISHDESK_LLM_TIMEOUT`` as a float of seconds in ``(0, 600]``.

    Unset / blank -> :data:`_DEFAULT_TIMEOUT_SECONDS`. Non-numeric, ``<= 0``,
    non-finite, or ``> _MAX_TIMEOUT_SECONDS`` -> :class:`~commishdesk.errors.NarratorError`
    (fail loud at load, the ``llmconfig`` precedent for a malformed
    ``COMMISHDESK_LLM_*`` value). The upper bound keeps the retry cap from
    compounding into a multi-hour stall (FR-40).
    """
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise NarratorError(
            f"COMMISHDESK_LLM_TIMEOUT must be a positive number of seconds, got {raw!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise NarratorError(
            f"COMMISHDESK_LLM_TIMEOUT must be a positive, finite number of seconds, "
            f"got {raw!r}"
        )
    if value > _MAX_TIMEOUT_SECONDS:
        raise NarratorError(
            f"COMMISHDESK_LLM_TIMEOUT must be <= {_MAX_TIMEOUT_SECONDS:g} seconds, "
            f"got {raw!r}"
        )
    return value


def _parse_cost_ceiling(raw: str | None) -> float:
    """``COMMISHDESK_COST_CEILING_USD`` as a positive, finite float of US dollars.

    Unset / blank -> :data:`_DEFAULT_COST_CEILING_USD`. Non-numeric, ``<= 0``, or
    non-finite -> :class:`~commishdesk.errors.NarratorError` (fail loud at load —
    the same validation shape as :func:`_parse_timeout`, with no upper bound: a
    cost ceiling has no analogue to FR-40's multi-hour-stall concern).
    """
    if raw is None:
        return _DEFAULT_COST_CEILING_USD
    try:
        value = float(raw)
    except ValueError:
        raise NarratorError(
            f"COMMISHDESK_COST_CEILING_USD must be a positive number of US "
            f"dollars, got {raw!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise NarratorError(
            f"COMMISHDESK_COST_CEILING_USD must be a positive, finite number of "
            f"US dollars, got {raw!r}"
        )
    return value


def load_llm_config(env: Mapping[str, str] = os.environ) -> LLMConfig:
    """Build the :class:`LLMConfig` from *env* (default :data:`os.environ`).

    *env* is only read, never written. Pass an explicit mapping to keep the real
    process environment out of a test.
    """

    def _get(name: str) -> str | None:
        value = env.get(name)
        return value.strip() or None if value is not None else None

    timeout = _parse_timeout(_get("COMMISHDESK_LLM_TIMEOUT"))
    cost_ceiling = _parse_cost_ceiling(_get("COMMISHDESK_COST_CEILING_USD"))

    raw_verifier = _get("COMMISHDESK_LLM_VERIFIER")
    verifier: LLMModelConfig | None
    if raw_verifier is not None and raw_verifier.casefold() in _VERIFIER_OFF:
        verifier = None
    else:
        default_spec = f"{_DEFAULT_VERIFIER_CONFIG.provider}:{_DEFAULT_VERIFIER_CONFIG.model_id}"
        verifier = _parse_model_spec(
            raw_verifier or default_spec,
            var="COMMISHDESK_LLM_VERIFIER",
            endpoint=_get("COMMISHDESK_LLM_VERIFIER_ENDPOINT"),
            timeout=timeout,
        )

    return LLMConfig(
        primary=_parse_model_spec(
            _get("COMMISHDESK_LLM_PRIMARY") or _DEFAULT_PRIMARY,
            var="COMMISHDESK_LLM_PRIMARY",
            endpoint=_get("COMMISHDESK_LLM_PRIMARY_ENDPOINT"),
            timeout=timeout,
        ),
        fallback=_parse_model_spec(
            _get("COMMISHDESK_LLM_FALLBACK") or _DEFAULT_FALLBACK,
            var="COMMISHDESK_LLM_FALLBACK",
            endpoint=_get("COMMISHDESK_LLM_FALLBACK_ENDPOINT"),
            timeout=timeout,
        ),
        cost_ceiling_usd=cost_ceiling,
        verifier=verifier,
    )
