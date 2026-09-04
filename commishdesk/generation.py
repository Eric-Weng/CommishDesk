"""The Generation Set — the one constructor that derives a run list (AD-6 / I1).

:func:`build_generation_set` is the **only** function that turns a pool of
candidate league ids into the list of leagues a run will process, and
:class:`GenerationSet` is instantiated in exactly one place — here. No other code
path adds a league to a run (invariant I1): mass-registering ten thousand
harvested ids, none of them activated, produces an empty set and zero downstream
work.

``is_activated`` is the seam Epic 7 fills with real verified-channel derivation
(≥1 confirmed email claim + an active validated delivery destination + headroom
under the activation budget). At MVP an explicit ``--league`` argument on the CLI
*is* the verified channel, so :func:`_cli_activation` returns ``True``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

__all__ = ["GenerationSet", "build_generation_set"]


@dataclass(frozen=True)
class GenerationSet:
    """The immutable run list: the league ids a run will process, in order, with
    no duplicates."""

    league_ids: tuple[str, ...]


def _cli_activation(_league_id: str) -> bool:
    """MVP activation: an explicit CLI ``--league`` argument is itself the
    verified channel. Epic 7 replaces this with real verified-channel state."""
    return True


def build_generation_set(
    candidates: Iterable[str],
    *,
    is_activated: Callable[[str], bool] = _cli_activation,
) -> GenerationSet:
    """Derive the :class:`GenerationSet` from ``candidates``, keeping only the
    activated ids, de-duplicated, in first-seen order.

    Empty ``candidates`` -> empty set. Every candidate deactivated -> empty set.
    """
    kept: dict[str, None] = {}
    for candidate in candidates:
        league_id = str(candidate)
        if league_id not in kept and is_activated(league_id):
            kept[league_id] = None
    return GenerationSet(league_ids=tuple(kept))
