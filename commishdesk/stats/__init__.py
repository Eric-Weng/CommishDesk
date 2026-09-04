"""Stage 2 — deterministic statistics (all-play, luck, coaching efficiency,
power-model score, draft grades); no model, no clock, no network."""

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
_GRADE_NAMES = frozenset(
    {
        "GRADE_METHOD",
        "THIRTEEN_POINT_SCALE",
        "DraftGrades",
        "GradeMethod",
        "TeamGrade",
        "compute_draft_grades",
    }
)

__all__ = sorted(_DRAFT_NAMES | _CONSENSUS_NAMES | _GRADE_NAMES)

if TYPE_CHECKING:
    # Explicit "as X" re-export idiom: these names are only for static type
    # checkers (the runtime attribute is served lazily via __getattr__ below),
    # so mark each import as an intentional re-export rather than unused (F401).
    from .consensus import ConsensusMetrics as ConsensusMetrics
    from .consensus import PickConsensus as PickConsensus
    from .consensus import PickRef as PickRef
    from .consensus import TeamConsensus as TeamConsensus
    from .consensus import compute_consensus_metrics as compute_consensus_metrics
    from .draft import MIN_RUN as MIN_RUN
    from .draft import BoardMetrics as BoardMetrics
    from .draft import PositionalRun as PositionalRun
    from .draft import TeamBoard as TeamBoard
    from .draft import compute_board_metrics as compute_board_metrics
    from .grades import GRADE_METHOD as GRADE_METHOD
    from .grades import THIRTEEN_POINT_SCALE as THIRTEEN_POINT_SCALE
    from .grades import DraftGrades as DraftGrades
    from .grades import GradeMethod as GradeMethod
    from .grades import TeamGrade as TeamGrade
    from .grades import compute_draft_grades as compute_draft_grades


def __getattr__(name: str) -> object:
    if name in _DRAFT_NAMES:
        from . import draft

        return getattr(draft, name)
    if name in _CONSENSUS_NAMES:
        from . import consensus

        return getattr(consensus, name)
    if name in _GRADE_NAMES:
        from . import grades

        return getattr(grades, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
