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

An unknown provider token, or a value that is not ``<provider>:<model_id>``,
raises :class:`~commishdesk.errors.NarratorError` at load.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class LLMModelConfig:
    """One provider + model id, plus an optional base-URL override."""

    provider: Provider
    model_id: str
    endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """The two models the selector tries, in order: ``primary`` then ``fallback``."""

    primary: LLMModelConfig
    fallback: LLMModelConfig


def _parse_model_spec(spec: str, *, var: str, endpoint: str | None) -> LLMModelConfig:
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
        provider=cast(Provider, provider), model_id=model_id, endpoint=endpoint
    )


def load_llm_config(env: Mapping[str, str] = os.environ) -> LLMConfig:
    """Build the :class:`LLMConfig` from *env* (default :data:`os.environ`).

    *env* is only read, never written. Pass an explicit mapping to keep the real
    process environment out of a test.
    """

    def _get(name: str) -> str | None:
        value = env.get(name)
        return value.strip() or None if value is not None else None

    return LLMConfig(
        primary=_parse_model_spec(
            _get("COMMISHDESK_LLM_PRIMARY") or _DEFAULT_PRIMARY,
            var="COMMISHDESK_LLM_PRIMARY",
            endpoint=_get("COMMISHDESK_LLM_PRIMARY_ENDPOINT"),
        ),
        fallback=_parse_model_spec(
            _get("COMMISHDESK_LLM_FALLBACK") or _DEFAULT_FALLBACK,
            var="COMMISHDESK_LLM_FALLBACK",
            endpoint=_get("COMMISHDESK_LLM_FALLBACK_ENDPOINT"),
        ),
    )
