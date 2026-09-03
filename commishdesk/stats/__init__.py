"""Stage 2 — deterministic statistics (all-play, luck, coaching efficiency, power-model score, draft grades); no model, no clock, no network."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exported lazily: ``import commishdesk.stats`` must stay stdlib-only (the
# extension-zone wheel test imports this package on a site-packages-free path),
# while ``from commishdesk.stats import BoardMetrics`` still resolves. The compute
# code in ``draft.py`` / ``consensus.py`` pulls in pydantic.
_DRAFT_NAMES = frozenset(
    {"MIN_RUN", "BoardMetrics", "PositionalRun", "TeamBoard", "compute_board_metrics"}
)
_CONSENSUS_NAMES = frozenset(
    {
        "ConsensusMetrics",
        "PickConsensus",
        "PickRef",
        "TeamConsensus",
        "compute_consensus_metrics",
    }
)

__all__ = sorted(_DRAFT_NAMES | _CONSENSUS_NAMES)

if TYPE_CHECKING:
    from .consensus import (
        ConsensusMetrics,
        PickConsensus,
        PickRef,
        TeamConsensus,
        compute_consensus_metrics,
    )
    from .draft import (
        MIN_RUN,
        BoardMetrics,
        PositionalRun,
        TeamBoard,
        compute_board_metrics,
    )


def __getattr__(name: str) -> object:
    if name in _DRAFT_NAMES:
        from . import draft

        return getattr(draft, name)
    if name in _CONSENSUS_NAMES:
        from . import consensus

        return getattr(consensus, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
