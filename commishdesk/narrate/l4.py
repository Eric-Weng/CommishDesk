"""Stage 4 — the L4 classifier extra (AD-12 L4, AD-47): a local ONNX toxicity model
scoring each LLM-narrated weekly section.

``commishdesk[l4]`` adds ``onnxruntime`` and ``tokenizers``; both are imported only
inside :func:`load_l4_scorer`, and ``narrate/__init__.py`` does not re-export this
module, so ``import commishdesk`` loads neither (I4). The engine never downloads a
model: the caller hands :func:`load_l4_scorer` a directory holding ``model.onnx`` and
``tokenizer.json``.

:func:`screen_weekly_issue` takes any parsed LLM-narrated Issue (fresh from a
completion or parsed from stored text) and replaces each section scoring at or above
the ``l4.toml`` threshold with its template section (the AD-30 splice), returning the
screened Issue and the reverted section ids. It never holds the Issue: whole-Issue
hold stays with the L2 named-person + banned-category rule.

Fail closed: a missing extra, a missing or invalid model path, a load failure or a
scoring error raises :class:`L4UnavailableError`; the caller never ships unscreened
LLM prose when L4 was requested.
"""

from __future__ import annotations

import functools
import math
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from commishdesk.errors import NarratorError
from commishdesk.sections import effective_suppressions

if TYPE_CHECKING:
    from commishdesk.facts.schema import WeeklyNarration
    from commishdesk.narrate.weekly_template import WeeklyIssue, WeeklySection

__all__ = [
    "L4Config",
    "L4Scorer",
    "L4UnavailableError",
    "load_l4_config",
    "load_l4_scorer",
    "screen_weekly_issue",
]

_CONFIG_FILENAME = "l4.toml"
_MODEL_FILENAME = "model.onnx"
_TOKENIZER_FILENAME = "tokenizer.json"
_EXTRA_HINT = "install the optional extra: pip install 'commishdesk[l4]'"


class L4UnavailableError(NarratorError):
    """L4 was requested but cannot run: the extra is missing, the model path is
    missing or invalid, the model failed to load, or scoring failed. Raised before any
    LLM prose is returned — fail closed."""


class L4Scorer(Protocol):
    """Scores one section's text: the probability in ``[0, 1]`` of the unsafe class."""

    def score(self, text: str) -> float: ...


@dataclass(frozen=True)
class L4Config:
    """The ``l4.toml`` settings."""

    threshold: float
    positive_index: int
    max_length: int


def _parse_config(text: str) -> L4Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise L4UnavailableError(f"{_CONFIG_FILENAME} is not valid TOML ({exc})") from exc
    threshold = data.get("threshold")
    positive_index = data.get("positive_index")
    max_length = data.get("max_length")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.0 <= threshold <= 1.0:
        raise L4UnavailableError(f"{_CONFIG_FILENAME}: threshold must be a number in [0, 1]")
    if isinstance(positive_index, bool) or not isinstance(positive_index, int) or positive_index < 0:
        raise L4UnavailableError(f"{_CONFIG_FILENAME}: positive_index must be a non-negative integer")
    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
        raise L4UnavailableError(f"{_CONFIG_FILENAME}: max_length must be a positive integer")
    return L4Config(threshold=float(threshold), positive_index=positive_index, max_length=max_length)


@functools.lru_cache(maxsize=1)
def load_l4_config() -> L4Config:
    """The packaged ``l4.toml`` settings (``L4UnavailableError`` when unreadable or invalid)."""
    try:
        text = resources.files("commishdesk.narrate").joinpath(_CONFIG_FILENAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ModuleNotFoundError) as exc:
        raise L4UnavailableError(f"{_CONFIG_FILENAME} could not be read ({type(exc).__name__})") from exc
    return _parse_config(text)


class _OnnxScorer:
    """The real scorer: a ``tokenizers`` tokenizer feeding an ``onnxruntime`` session."""

    def __init__(self, session: Any, tokenizer: Any, np: Any, config: L4Config) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._np = np
        self._config = config
        self._input_names = [i.name for i in session.get_inputs()]

    def score(self, text: str) -> float:
        np = self._np
        try:
            encoding = self._tokenizer.encode(text)
            ids = np.asarray([encoding.ids], dtype=np.int64)
            mask = np.asarray([encoding.attention_mask], dtype=np.int64)
            candidates = {
                "input_ids": ids,
                "attention_mask": mask,
                "token_type_ids": np.zeros_like(ids),
            }
            feeds = {name: candidates[name] for name in self._input_names if name in candidates}
            logits = np.asarray(self._session.run(None, feeds)[0], dtype=np.float64).reshape(-1)
            if logits.size == 1:
                value = float(1.0 / (1.0 + np.exp(-logits[0])))
            else:
                shifted = np.exp(logits - logits.max())
                value = float((shifted / shifted.sum())[self._config.positive_index])
        except Exception as exc:  # noqa: BLE001 - any runtime failure is "L4 unavailable"
            raise L4UnavailableError(f"L4 scoring failed ({type(exc).__name__})") from exc
        if not math.isfinite(value):
            raise L4UnavailableError("L4 scoring failed (non-finite score)")
        return value


def load_l4_scorer(path: str | Path | None) -> L4Scorer:
    """Load the classifier from *path*, a directory holding ``model.onnx`` and
    ``tokenizer.json``. Nothing is downloaded.

    Raises :class:`L4UnavailableError` for a ``None`` / missing / invalid path, a
    missing ``commishdesk[l4]`` extra, or a model that fails to load.
    """
    if path is None:
        raise L4UnavailableError("L4 was requested but no model path was given")
    root = Path(path)
    if not root.is_dir():
        raise L4UnavailableError("L4 model path is not a directory")
    model = root / _MODEL_FILENAME
    tokenizer_file = root / _TOKENIZER_FILENAME
    if not model.is_file() or not tokenizer_file.is_file():
        raise L4UnavailableError(f"L4 model directory must hold {_MODEL_FILENAME} and {_TOKENIZER_FILENAME}")
    config = load_l4_config()
    try:
        import numpy as np
        import onnxruntime
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise L4UnavailableError(f"L4 needs onnxruntime and tokenizers; {_EXTRA_HINT}") from exc
    try:
        tokenizer = Tokenizer.from_file(str(tokenizer_file))
        tokenizer.no_padding()
        tokenizer.enable_truncation(max_length=config.max_length)
        session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 - corrupt model / tokenizer of any shape
        raise L4UnavailableError(f"L4 model failed to load ({type(exc).__name__})") from exc
    return _OnnxScorer(session, tokenizer, np, config)


def _section_text(section: WeeklySection) -> str:
    return "\n\n".join(section.blocks)


def screen_weekly_issue(
    issue: WeeklyIssue,
    narration: WeeklyNarration,
    scorer: L4Scorer,
    *,
    has_prior_week: bool | None = None,
    suppressed: Iterable[str] | None = None,
) -> tuple[WeeklyIssue, tuple[str, ...]]:
    """Score each LLM-written section of *issue*; revert each one at or above the
    threshold to its template section. Returns ``(screened issue, reverted ids)``.

    *issue* must be an LLM-narrated Issue (the caller never hands a template one).
    Sections whose id is in *suppressed* (template prose by AD-30) and sections
    outside the seven are never scored. The threshold is inclusive. A scorer error
    (or a non-finite score) raises :class:`L4UnavailableError` and nothing is returned.
    """
    from commishdesk.narrate.weekly_template import revert_sections_to_template

    if has_prior_week is None:
        has_prior_week = narration.league.week >= 2
    config = load_l4_config()
    skipped = effective_suppressions(suppressed, has_prior_week)
    flagged: list[str] = []
    for section in issue.sections:
        section_id = section.section_id
        if section_id is None or section_id in skipped:
            continue
        try:
            value = float(scorer.score(_section_text(section)))
        except L4UnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - a scorer that raises means L4 cannot vouch
            raise L4UnavailableError(f"L4 scoring failed ({type(exc).__name__})") from exc
        if not math.isfinite(value):
            raise L4UnavailableError("L4 scoring failed (non-finite score)")
        if value >= config.threshold:
            flagged.append(section_id)
    if not flagged:
        return issue, ()
    screened = revert_sections_to_template(issue, narration, flagged, has_prior_week=has_prior_week)
    return screened, tuple(flagged)
