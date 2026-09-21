"""Stage 2 — deterministic statistics (all-play/luck, coaching efficiency,
power-model score, draft grades); no model, no clock, no network.

All-play, expected wins, luck, and blowouts are implemented in
:mod:`commishdesk.stats.weekly` (Story 5.4); the optimal-lineup solver and
coaching efficiency it feeds are in :mod:`commishdesk.stats.lineup` (Story
5.5); the matchup-derived standings, the derived playoff picture and the
Sleeper cross-check are in :mod:`commishdesk.stats.standings`, the model
power rank and its per-week history in :mod:`commishdesk.stats.power`, and the
next-week stakes/game-of-the-week/bye impact in :mod:`commishdesk.stats.stakes`
and the transactions desk in :mod:`commishdesk.stats.transactions` (Story 5.6 /
5.7)."""

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
_LINEUP_NAMES = frozenset(
    {
        "BenchedPlayer",
        "ByeStarter",
        "LineupSlot",
        "TeamLineup",
        "WeeklyLineups",
        "compute_weekly_lineups",
    }
)
_STANDINGS_NAMES = frozenset(
    {
        "POINTS_FOR_ROUNDING_TOLERANCE",
        "TIEBREAK",
        "DivisionOrder",
        "PlayoffPicture",
        "Standings",
        "Streak",
        "TeamRecord",
        "TeamStanding",
        "WeekPoints",
        "bye_count",
        "compute_standings",
        "cross_check_standings",
        "regular_season_records",
    }
)
_POWER_NAMES = frozenset(
    {
        "POWER_NUDGE_CAP",
        "POWER_WEIGHTS",
        "PowerRanks",
        "TeamPower",
        "compute_power_history",
        "compute_power_ranks",
    }
)
_STAKES_NAMES = frozenset(
    {
        "ByeImpact",
        "GameOfWeek",
        "NextWeek",
        "NextWeekCard",
        "NextWeekSide",
        "compute_next_week",
    }
)
_TRANSACTIONS_NAMES = frozenset(
    {
        "RECENT_TRADE_WEEKS",
        "MarketNote",
        "Move",
        "Trade",
        "TradeSide",
        "TransactionsDesk",
        "compute_transactions_desk",
    }
)
_WEEKLY_NAMES = frozenset(
    {
        "BLOWOUT_RATIO",
        "MEANINGFUL_FROM_WEEK",
        "AllPlayRecord",
        "Period",
        "TeamGame",
        "TeamPoints",
        "TeamWeekStats",
        "WeekMargin",
        "WeekSummary",
        "WeeklyStats",
        "compute_weekly_stats",
    }
)

__all__ = sorted(
    _CONSENSUS_NAMES
    | _DRAFT_NAMES
    | _GRADE_NAMES
    | _LINEUP_NAMES
    | _POWER_NAMES
    | _STAKES_NAMES
    | _STANDINGS_NAMES
    | _TRANSACTIONS_NAMES
    | _WEEKLY_NAMES
)

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
    from .lineup import BenchedPlayer as BenchedPlayer
    from .lineup import ByeStarter as ByeStarter
    from .lineup import LineupSlot as LineupSlot
    from .lineup import TeamLineup as TeamLineup
    from .lineup import WeeklyLineups as WeeklyLineups
    from .lineup import compute_weekly_lineups as compute_weekly_lineups
    from .power import POWER_NUDGE_CAP as POWER_NUDGE_CAP
    from .power import POWER_WEIGHTS as POWER_WEIGHTS
    from .power import PowerRanks as PowerRanks
    from .power import TeamPower as TeamPower
    from .power import compute_power_history as compute_power_history
    from .power import compute_power_ranks as compute_power_ranks
    from .stakes import ByeImpact as ByeImpact
    from .stakes import GameOfWeek as GameOfWeek
    from .stakes import NextWeek as NextWeek
    from .stakes import NextWeekCard as NextWeekCard
    from .stakes import NextWeekSide as NextWeekSide
    from .stakes import compute_next_week as compute_next_week
    from .standings import POINTS_FOR_ROUNDING_TOLERANCE as POINTS_FOR_ROUNDING_TOLERANCE
    from .standings import TIEBREAK as TIEBREAK
    from .standings import DivisionOrder as DivisionOrder
    from .standings import PlayoffPicture as PlayoffPicture
    from .standings import Standings as Standings
    from .standings import Streak as Streak
    from .standings import TeamRecord as TeamRecord
    from .standings import TeamStanding as TeamStanding
    from .standings import WeekPoints as WeekPoints
    from .standings import bye_count as bye_count
    from .standings import compute_standings as compute_standings
    from .standings import cross_check_standings as cross_check_standings
    from .standings import regular_season_records as regular_season_records
    from .transactions import RECENT_TRADE_WEEKS as RECENT_TRADE_WEEKS
    from .transactions import MarketNote as MarketNote
    from .transactions import Move as Move
    from .transactions import Trade as Trade
    from .transactions import TradeSide as TradeSide
    from .transactions import TransactionsDesk as TransactionsDesk
    from .transactions import compute_transactions_desk as compute_transactions_desk
    from .weekly import BLOWOUT_RATIO as BLOWOUT_RATIO
    from .weekly import MEANINGFUL_FROM_WEEK as MEANINGFUL_FROM_WEEK
    from .weekly import AllPlayRecord as AllPlayRecord
    from .weekly import Period as Period
    from .weekly import TeamGame as TeamGame
    from .weekly import TeamPoints as TeamPoints
    from .weekly import TeamWeekStats as TeamWeekStats
    from .weekly import WeeklyStats as WeeklyStats
    from .weekly import WeekMargin as WeekMargin
    from .weekly import WeekSummary as WeekSummary
    from .weekly import compute_weekly_stats as compute_weekly_stats


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
    if name in _LINEUP_NAMES:
        from . import lineup

        return getattr(lineup, name)
    if name in _STANDINGS_NAMES:
        from . import standings

        return getattr(standings, name)
    if name in _POWER_NAMES:
        from . import power

        return getattr(power, name)
    if name in _STAKES_NAMES:
        from . import stakes

        return getattr(stakes, name)
    if name in _TRANSACTIONS_NAMES:
        from . import transactions

        return getattr(transactions, name)
    if name in _WEEKLY_NAMES:
        from . import weekly

        return getattr(weekly, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
