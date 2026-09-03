"""Stage 2 — deterministic statistics (all-play, luck, coaching efficiency, power-model score, draft grades); no model, no clock, no network."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exported lazily: ``import commishdesk.stats`` must stay stdlib-only (the
# extension-zone wheel test imports this package on a site-packages-free path),
# while ``from commishdesk.stats import BoardMetrics`` still resolves. The compute
# code in ``draft.py`` pulls in pydantic.
__all__ = [
    "MIN_RUN",
    "BoardMetrics",
    "PositionalRun",
    "TeamBoard",
    "compute_board_metrics",
]

if TYPE_CHECKING:
    from .draft import (
        MIN_RUN,
        BoardMetrics,
        PositionalRun,
        TeamBoard,
        compute_board_metrics,
    )


def __getattr__(name: str) -> object:
    if name in __all__:
        from . import draft

        return getattr(draft, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
