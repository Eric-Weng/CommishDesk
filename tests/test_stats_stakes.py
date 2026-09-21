"""Story 5.7: ``compute_next_week`` -- next week's cards, stakes, the game of the
week, and bye impact.

One test per row of the spec's I/O & Edge-Case Matrix: the week-10 preview on the
committed fixture (cards, ranks, records, the one game of the week, a bye-flagged
starter), the conservative clinch/elimination rules on hand-built leagues (and
the tie on the line, and the division cases, and the stand-downs), plus
determinism, the "no hardcoded league shape" guard, and the import-fence pair
that keeps ``stats/stakes.py`` off the network, the store, and every later
pipeline stage.

Reconciliation mirrors ``tests/test_stats_standings.py``. The always-on CI oracle
is the committed fixture itself (the six week-10 pairings the spec's matrix
pins) plus the hand-built leagues; the phase-0 golden
(``brief/phase-0/week10-facts.json``) is a private planning artifact that is not
committed to this repo (CLAUDE.md s1).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.ingest import (
    Division,
    Draft,
    LeagueFormat,
    LeagueModel,
    Matchup,
    PlayerSnapshot,
    PlayoffFormat,
    Roster,
    Team,
    WeekModel,
    build_league_model,
    build_player_snapshot,
    build_week_model,
)
from commishdesk.stats import (
    ByeImpact,
    NextWeek,
    NextWeekCard,
    NextWeekSide,
    PowerRanks,
    Standings,
    TeamPower,
    TeamStanding,
    compute_next_week,
    compute_power_ranks,
    compute_standings,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
STATS_STAKES = REPO_ROOT / "commishdesk" / "stats" / "stakes.py"

WEEK10 = "week10-blowout.json"

#: The two NFL teams on bye in week 11 of the committed league's season -- the
#: pair the spec's matrix names. ``ingest/nfl_byes.toml`` carries no 2025 table,
#: so the caller supplies them, exactly as ``compute_weekly_lineups`` takes its
#: byes from the caller.
BYES_NEXT_WEEK = frozenset({"IND", "NO"})

#: The six week-10 -> week-11 pairings, copied from the spec's I/O & Edge-Case
#: Matrix (and cross-checked against the phase-0 golden's ``matchups.next_week``).
_WEEK10_CARDS = (
    ("1", "11"),
    ("5", "8"),
    ("4", "6"),
    ("3", "9"),
    ("2", "12"),
    ("7", "10"),
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run_fixture(name: str, byes: frozenset[str] | None = BYES_NEXT_WEEK) -> NextWeek:
    bundle = _bundle(name)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    return compute_next_week(
        week,
        league,
        compute_standings(week, league),
        compute_power_ranks(week),
        build_player_snapshot(bundle),
        byes,
    )


def _league(
    *,
    playoff_teams: int | None = 6,
    divisions: list[Division] | None = None,
    teams: list[Team] | None = None,
) -> LeagueModel:
    return LeagueModel(
        league_id="id_league",
        name="Test League",
        season=2025,
        platform="sleeper",
        format=LeagueFormat(
            team_count=12,
            roster_slots=["QB", "RB", "WR", "TE", "FLEX", "BN"],
            flex_eligibility={"FLEX": ["RB", "WR", "TE"]},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            divisions=list(divisions or []),
            playoff=None if playoff_teams is None else PlayoffFormat(bracket_teams=playoff_teams),
        ),
        teams=list(teams or []),
        picks=[],
        draft=Draft(id="id_draft"),
    )


def _standings(records: dict[str, tuple[int, int, int]], *, week: int, through: int) -> Standings:
    teams: list[TeamStanding] = []
    for index, roster_id in enumerate(sorted(records, key=lambda rid: int(rid)), start=1):
        wins, losses, ties = records[roster_id]
        games = wins + losses + ties
        teams.append(
            TeamStanding(
                roster_id=roster_id,
                wins=wins,
                losses=losses,
                ties=ties,
                win_pct=round((wins + 0.5 * ties) / games, 3) if games else 0.0,
                points_for=0.0,
                points_against=0.0,
                rank=index,
                division_rank=None,
                streak=None,
                high_week=None,
                low_week=None,
            )
        )
    return Standings(
        week=week,
        through_week=through,
        regular_season_complete=False,
        teams=teams,
        divisions=[],
        playoff_picture=None,
    )


def _power(roster_ids: list[str], *, week: int) -> PowerRanks:
    ordered = sorted(roster_ids, key=lambda rid: int(rid))
    return PowerRanks(
        week=week,
        teams=[
            TeamPower(
                roster_id=roster_id,
                model_score=None,
                model_rank=index,
                all_play_pct=None,
                normalized_avg_pf=None,
                win_pct=None,
            )
            for index, roster_id in enumerate(ordered, start=1)
        ],
    )


def _pair_rows(pairs: list[tuple[int, int]], week: int) -> list[Matchup]:
    rows: list[Matchup] = []
    for index, (a, b) in enumerate(pairs, start=1):
        rows.append(Matchup(week=week, roster_id=str(a), matchup_id=index, opponent_roster_id=str(b)))
        rows.append(Matchup(week=week, roster_id=str(b), matchup_id=index, opponent_roster_id=str(a)))
    return rows


def _scenario(
    *,
    records: dict[str, tuple[int, int, int]],
    pairs: list[tuple[int, int]],
    week: int = 7,
    playoff_week_start: int | None = 10,
    league: LeagueModel | None = None,
    matchups: list[Matchup] | None = None,
    players: dict[str, PlayerSnapshot] | None = None,
    byes: frozenset[str] | None = None,
    extra_next: list[Matchup] | None = None,
) -> NextWeek:
    ids = sorted(records, key=lambda rid: int(rid))
    week_model = WeekModel(
        week=week,
        rosters=[Roster(roster_id=roster_id) for roster_id in ids],
        matchups=list(matchups or []),
        transactions=[],
        playoff_week_start=playoff_week_start,
        next_matchups=_pair_rows(pairs, week + 1) + list(extra_next or []),
    )
    return compute_next_week(
        week_model,
        league or _league(),
        _standings(records, week=week, through=week),
        _power(ids, week=week),
        dict(players or {}),
        byes,
    )


def _sides(result: NextWeek) -> dict[str, NextWeekSide]:
    sides: dict[str, NextWeekSide] = {}
    for card in result.cards:
        sides[card.a.roster_id] = card.a
        sides[card.b.roster_id] = card.b
    return sides


def _card_for(result: NextWeek, roster_id: str) -> NextWeekCard:
    return next(
        card for card in result.cards if roster_id in (card.a.roster_id, card.b.roster_id)
    )


# --------------------------------------------------------------------------- #
# Row: week-10 preview
# --------------------------------------------------------------------------- #


def test_week10_preview_pairs_the_six_cards() -> None:
    result = _run_fixture(WEEK10)
    got = {frozenset((card.a.roster_id, card.b.roster_id)) for card in result.cards}
    assert got == {frozenset(pair) for pair in _WEEK10_CARDS}
    assert len(result.cards) == len(_WEEK10_CARDS)
    # Cards are ordered by matchup_id; `a` is the lower roster id.
    assert [(card.a.roster_id, card.b.roster_id) for card in result.cards] == [
        ("1", "11"),
        ("5", "8"),
        ("4", "6"),
        ("3", "9"),
        ("2", "12"),
        ("7", "10"),
    ]


def test_week10_preview_ranks_and_records_track_standings_and_power() -> None:
    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    standings = compute_standings(week, league)
    power = compute_power_ranks(week)
    result = compute_next_week(
        week, league, standings, power, build_player_snapshot(bundle), BYES_NEXT_WEEK
    )

    standing_by_id = {team.roster_id: team for team in standings.teams}
    rank_by_id = {team.roster_id: team.model_rank for team in power.teams}
    for card in result.cards:
        for side in (card.a, card.b):
            expected = standing_by_id[side.roster_id]
            assert (side.wins, side.losses, side.ties) == (
                expected.wins,
                expected.losses,
                expected.ties,
            )
            assert side.model_rank == rank_by_id[side.roster_id]


#: Copied from the phase-0 golden's ``matchups.next_week`` (``a_power_rank`` /
#: ``b_power_rank`` / ``a_record`` / ``b_record``), so the always-on suite pins
#: the matrix's "equal the golden's" without reading the private file.
_WEEK10_GOLDEN_SIDES = {
    "1": (2, (7, 3, 0)),
    "11": (11, (2, 8, 0)),
    "8": (5, (4, 6, 0)),
    "5": (8, (6, 4, 0)),
    "6": (6, (5, 5, 0)),
    "4": (7, (4, 6, 0)),
    "9": (3, (6, 4, 0)),
    "3": (4, (7, 3, 0)),
    "2": (1, (9, 1, 0)),
    "12": (10, (5, 5, 0)),
    "10": (9, (3, 7, 0)),
    "7": (12, (2, 8, 0)),
}


def test_week10_preview_ranks_and_records_equal_the_golden() -> None:
    result = _run_fixture(WEEK10)
    sides = _sides(result)
    assert set(sides) == set(_WEEK10_GOLDEN_SIDES)
    for roster_id, (rank, record) in _WEEK10_GOLDEN_SIDES.items():
        side = sides[roster_id]
        assert side.model_rank == rank, roster_id
        assert (side.wins, side.losses, side.ties) == record, roster_id


def test_week10_preview_has_exactly_one_game_of_the_week() -> None:
    result = _run_fixture(WEEK10)
    assert result.game_of_week is not None
    assert result.game_of_week.stakes
    assert result.game_of_week.rule == "most_stakes"
    marked = [card for card in result.cards if card.game_of_week]
    assert len(marked) == 1
    assert marked[0].matchup_id == result.game_of_week.matchup_id
    assert marked[0].stakes == result.game_of_week.stakes
    # The phase-0 golden's marquee is rosters 9 v 3 ("a first-round bye still in
    # reach for both"); the documented rule lands on the same card.
    assert {marked[0].a.roster_id, marked[0].b.roster_id} == {"3", "9"}
    assert result.game_of_week.rank_sum == 7


def test_week10_preview_flags_a_starter_on_bye_next_week() -> None:
    result = _run_fixture(WEEK10)
    entries = [entry for card in result.cards for entry in (card.bye_impact or [])]
    assert entries, "the week-10 fixture must start at least one player on a week-11 bye"
    assert {entry.nfl_team for entry in entries} <= BYES_NEXT_WEEK
    for card in result.cards:
        for entry in card.bye_impact or []:
            assert entry.roster_id in (card.a.roster_id, card.b.roster_id)


# --------------------------------------------------------------------------- #
# Row: clinch / elimination, and the tie on the line
# --------------------------------------------------------------------------- #

#: Two games left, six make the bracket, two get byes: teams 1-2 are far clear,
#: 3-5 are clear, 6 is on the bubble, 7-11 are still mathematically alive, and
#: 12 is done.
_RECORDS = {
    "1": (20, 0, 0),
    "2": (20, 0, 0),
    "3": (10, 0, 0),
    "4": (10, 0, 0),
    "5": (10, 0, 0),
    "6": (5, 0, 0),
    "7": (4, 0, 0),
    "8": (4, 0, 0),
    "9": (4, 0, 0),
    "10": (4, 0, 0),
    "11": (4, 0, 0),
    "12": (0, 0, 0),
}
_PAIRS = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12)]


def test_clinch_and_elimination_follow_the_conservative_rules() -> None:
    result = _scenario(records=_RECORDS, pairs=_PAIRS)
    sides = _sides(result)

    assert sides["1"].clinched_playoff and sides["1"].clinched_bye
    assert sides["3"].clinched_playoff and not sides["3"].clinched_bye
    assert not sides["6"].clinched_playoff and not sides["6"].eliminated
    assert sides["12"].eliminated and not sides["12"].clinched_playoff

    assert "wildcard_race" in _card_for(result, "6").stakes
    assert "draft_position" in _card_for(result, "12").stakes


def test_a_team_level_with_n_others_is_neither_clinched_nor_eliminated() -> None:
    records = {str(i): (7, 0, 0) for i in range(1, 7)} | {str(i): (5, 0, 0) for i in range(7, 13)}
    result = _scenario(records=records, pairs=_PAIRS)
    sides = _sides(result)

    # roster 7's ceiling (5 + 2 = 7) is exactly the six leaders' current total.
    assert not sides["7"].eliminated
    # and the leaders' current total is exactly five others' ceiling.
    assert not sides["1"].clinched_playoff


def test_the_elimination_tag_marks_a_team_a_loss_would_knock_out() -> None:
    records = {str(i): (7, 0, 0) for i in range(1, 7)} | {str(i): (5, 0, 0) for i in range(7, 13)}
    result = _scenario(records=records, pairs=_PAIRS)

    assert "elimination" in _card_for(result, "7").stakes


# --------------------------------------------------------------------------- #
# Row: no divisions
# --------------------------------------------------------------------------- #


def test_no_divisions_never_emits_a_division_tag() -> None:
    result = _scenario(records=_RECORDS, pairs=_PAIRS)
    for card in result.cards:
        assert "division_race" not in card.stakes
        assert not card.a.clinched_division and not card.b.clinched_division


def test_division_race_needs_a_still_winnable_division() -> None:
    records = {str(i): (10, 0, 0) for i in range(1, 13)}
    divisions = [Division(id=1, name="East"), Division(id=2, name="West")]
    teams = [
        Team(roster_id=str(i), division_id=(1 if i <= 6 else 2)) for i in range(1, 13)
    ]
    result = _scenario(
        records=records, pairs=_PAIRS, league=_league(divisions=divisions, teams=teams)
    )
    sides = _sides(result)

    assert not sides["1"].clinched_division
    assert any("division_race" in card.stakes for card in result.cards)


def test_a_clinched_division_suppresses_the_division_race() -> None:
    records = {"1": (20, 0, 0)} | {str(i): (0, 0, 0) for i in range(2, 13)}
    divisions = [Division(id=1, name="Only")]
    teams = [Team(roster_id=str(i), division_id=1) for i in range(1, 13)]
    result = _scenario(
        records=records, pairs=_PAIRS, league=_league(divisions=divisions, teams=teams)
    )

    assert _sides(result)["1"].clinched_division
    assert "division_race" not in _card_for(result, "1").stakes


# --------------------------------------------------------------------------- #
# Row: no playoff format
# --------------------------------------------------------------------------- #


def test_no_playoff_format_stands_the_playoff_tags_down() -> None:
    result = _scenario(records=_RECORDS, pairs=_PAIRS, league=_league(playoff_teams=None))
    for card in result.cards:
        assert "bye_seed" not in card.stakes
        assert "wildcard_race" not in card.stakes
        assert "draft_position" not in card.stakes
        assert "elimination" not in card.stakes
        assert not card.a.eliminated and not card.a.clinched_playoff and not card.a.clinched_bye
        assert not card.b.eliminated and not card.b.clinched_playoff and not card.b.clinched_bye


# --------------------------------------------------------------------------- #
# Row: the final regular week / no schedule
# --------------------------------------------------------------------------- #


def test_the_final_regular_week_stands_the_stakes_down_but_keeps_the_cards() -> None:
    records = {str(i): (10, 0, 0) for i in range(1, 13)}
    result = _scenario(records=records, pairs=_PAIRS, week=9, playoff_week_start=10)

    assert len(result.cards) == len(_PAIRS)
    assert all(card.stakes == [] for card in result.cards)
    assert result.game_of_week is not None
    assert result.game_of_week.stakes == []
    assert result.game_of_week.rule == "top_ranks"
    for card in result.cards:
        assert card.a.model_rank is not None and card.b.model_rank is not None
        assert (card.a.wins, card.a.losses, card.a.ties) == (10, 0, 0)


def test_the_final_regular_week_still_reports_the_clinch_and_elimination_flags() -> None:
    # Only the tags stand down: with no games left the flags are the final
    # regular-season picture, and the Issue still needs to say who is in or out.
    records = {str(i): (13 - i, i - 1, 0) for i in range(1, 13)}
    result = _scenario(records=records, pairs=_PAIRS, week=9, playoff_week_start=10)
    sides = _sides(result)

    assert all(card.stakes == [] for card in result.cards)
    assert sides["1"].clinched_playoff and sides["1"].clinched_bye
    assert sides["3"].clinched_playoff and not sides["3"].clinched_bye
    assert sides["12"].eliminated and not sides["12"].clinched_playoff


def test_no_schedule_means_no_cards_and_no_game_of_the_week() -> None:
    records = {str(i): (10, 0, 0) for i in range(1, 13)}
    week_model = WeekModel(
        week=7,
        rosters=[Roster(roster_id=str(i)) for i in range(1, 13)],
        matchups=[],
        transactions=[],
        playoff_week_start=10,
        next_matchups=[],
    )
    result = compute_next_week(
        week_model,
        _league(),
        _standings(records, week=7, through=7),
        _power(list(records), week=7),
        {},
    )
    assert result.cards == []
    assert result.game_of_week is None


# --------------------------------------------------------------------------- #
# Row: bye impact
# --------------------------------------------------------------------------- #


def _two_roster_week(*, players: dict[str, PlayerSnapshot], byes: frozenset[str] | None) -> NextWeek:
    records = {"1": (10, 0, 0), "2": (10, 0, 0)}
    week_model = WeekModel(
        week=7,
        rosters=[Roster(roster_id="1"), Roster(roster_id="2")],
        matchups=[
            Matchup(week=7, roster_id="1", starters=["p1", "p2"]),
            Matchup(week=7, roster_id="2", starters=["p3"]),
        ],
        transactions=[],
        playoff_week_start=10,
        next_matchups=_pair_rows([(1, 2)], 8),
    )
    return compute_next_week(
        week_model,
        _league(),
        _standings(records, week=7, through=7),
        _power(["1", "2"], week=7),
        players,
        byes,
    )


def test_bye_impact_names_the_week_n_starter_on_bye() -> None:
    players = {
        "p1": PlayerSnapshot(player_id="p1", position="RB", nfl_team="KC"),
        "p2": PlayerSnapshot(player_id="p2", position="WR", nfl_team="BUF"),
        "p3": PlayerSnapshot(player_id="p3", position="QB", nfl_team="KC"),
    }
    result = _two_roster_week(players=players, byes=frozenset({"KC"}))
    assert result.cards[0].bye_impact == [
        ByeImpact(roster_id="1", player_id="p1", nfl_team="KC", position="RB"),
        ByeImpact(roster_id="2", player_id="p3", nfl_team="KC", position="QB"),
    ]


def test_absent_bye_data_leaves_bye_impact_unknown() -> None:
    result = _two_roster_week(players={}, byes=None)
    assert result.cards[0].bye_impact is None


def test_bye_data_with_nobody_on_bye_is_an_empty_list() -> None:
    result = _two_roster_week(players={}, byes=frozenset({"KC"}))
    assert result.cards[0].bye_impact == []


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_deterministic_on_the_committed_fixture() -> None:
    first = _run_fixture(WEEK10)
    second = _run_fixture(WEEK10)
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


def test_deterministic_on_hand_built_input() -> None:
    first = _scenario(records=_RECORDS, pairs=_PAIRS)
    second = _scenario(records=_RECORDS, pairs=_PAIRS)
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- #
# No hardcoded league shape
# --------------------------------------------------------------------------- #


def test_stakes_source_carries_no_literal_bracket_size_or_roster_count() -> None:
    """AC: no bracket size, bye count or division count appears as a literal in
    ``stats/stakes.py`` -- every one of them is read from
    ``league.format.playoff`` / ``league.format.divisions``. (The one integer
    that *is* a literal, ``_CARD_SIDES``, is a matchup's roster count, not the
    league's shape.)"""
    tree = ast.parse(STATS_STAKES.read_text(encoding="utf-8"))
    integers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }
    assert 6 not in integers  # the declared bracket size
    assert 12 not in integers  # the roster count
    assert 3 not in integers  # the declared division count


# --------------------------------------------------------------------------- #
# Import fence: stats/stakes.py never reaches a later pipeline stage
# --------------------------------------------------------------------------- #


def _imported_dotted_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level == 1:
                names.add(f"commishdesk.stats.{node.module}" if node.module else "commishdesk.stats")
            elif node.level == 2:
                names.add(node.module or "commishdesk")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return names


def test_stakes_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_STAKES)
    forbidden_roots = {"adapters", "store", "facts", "narrate", "render", "deliver", "statmods"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"] and len(name.split(".")) > 1 and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported


def test_stakes_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_STAKES)
    banned = {"datetime", "time", "os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_stakes_output_carries_no_player_display_name_field() -> None:
    """AC: no ``stats/`` output carries a player display name -- Story 5.8 joins
    those. Checked on the exported models' field sets, not just the source."""
    for cls in (ByeImpact, NextWeekSide, NextWeekCard, NextWeek):
        assert "name" not in cls.model_fields
        assert "display_name" not in cls.model_fields
        assert "manager" not in cls.model_fields


def test_stakes_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.stats as stats

    assert stats.compute_next_week is compute_next_week
    for name in (
        "ByeImpact",
        "GameOfWeek",
        "NextWeek",
        "NextWeekCard",
        "NextWeekSide",
        "compute_next_week",
    ):
        assert name in stats.__all__


@pytest.mark.parametrize("forbidden", ["name", "display_name"])
def test_stakes_source_never_references_a_display_name(forbidden: str) -> None:
    tree = ast.parse(STATS_STAKES.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert forbidden not in names | attrs



# --------------------------------------------------------------------------- #
# Review patches: the boundaries the first pass left unpinned
# --------------------------------------------------------------------------- #


def test_bye_seed_marks_a_reachable_bye_and_never_a_clinched_or_unreachable_one() -> None:
    # Two games left, six in, two byes. Roster 1 leads but a bye is not yet safe.
    records = {
        "1": (8, 0, 0),
        "2": (7, 0, 0),
        "3": (7, 0, 0),
        "4": (6, 0, 0),
        "5": (6, 0, 0),
        "6": (5, 0, 0),
    } | {str(i): (0, 0, 0) for i in range(7, 13)}
    result = _scenario(records=records, pairs=_PAIRS)

    assert not _sides(result)["1"].clinched_bye
    assert "bye_seed" in _card_for(result, "1").stakes
    # Roster 12 cannot reach a bye (six rosters are strictly above its ceiling).
    assert "bye_seed" not in _card_for(result, "12").stakes

    # Rosters 1 and 2 have clinched the byes in ``_RECORDS``: nothing left to seed.
    clinched = _scenario(records=_RECORDS, pairs=_PAIRS)
    assert _sides(clinched)["1"].clinched_bye
    assert "bye_seed" not in _card_for(clinched, "1").stakes


def test_elimination_needs_exactly_n_others_strictly_above_the_ceiling() -> None:
    # Roster 12's ceiling is 0 + 2 games = 2. Six others at 3 wins are strictly above it.
    six_above = {str(i): (3, 0, 0) for i in range(1, 7)} | {str(i): (0, 0, 0) for i in range(7, 13)}
    assert _sides(_scenario(records=six_above, pairs=_PAIRS))["12"].eliminated

    # Five above is not enough.
    five_above = {str(i): (3, 0, 0) for i in range(1, 6)} | {str(i): (0, 0, 0) for i in range(6, 13)}
    assert not _sides(_scenario(records=five_above, pairs=_PAIRS))["12"].eliminated

    # Six exactly level with the ceiling (2 wins) are not *strictly* above it.
    six_level = {str(i): (2, 0, 0) for i in range(1, 7)} | {str(i): (0, 0, 0) for i in range(7, 13)}
    assert not _sides(_scenario(records=six_level, pairs=_PAIRS))["12"].eliminated


def test_an_eliminated_pairing_carries_only_the_draft_position_tag() -> None:
    six_above = {str(i): (3, 0, 0) for i in range(1, 7)} | {str(i): (0, 0, 0) for i in range(7, 13)}
    result = _scenario(records=six_above, pairs=_PAIRS)

    card = _card_for(result, "11")
    assert card.a.eliminated and card.b.eliminated
    assert card.stakes == ["draft_position"]


def test_a_half_win_tie_counts_toward_the_ceiling_and_the_current_total() -> None:
    # Roster 12: 1 win + 2 ties banks 2.0, so its ceiling is 4.0; six rosters at
    # 5 wins are strictly above it. Counting a tie as a full win would put the
    # ceiling at 5.0 and lift the elimination.
    records = {str(i): (5, 0, 0) for i in range(1, 7)} | {str(i): (0, 0, 0) for i in range(7, 12)}
    records["12"] = (1, 0, 2)
    assert _sides(_scenario(records=records, pairs=_PAIRS))["12"].eliminated


def _two_division_league() -> LeagueModel:
    """Rosters 1 and 2 share a division; every other roster sits alone in its own,
    so it has no rival and carries no division tag that could mask 1's or 2's."""
    divisions = [Division(id=i, name=f"D{i}") for i in range(1, 12)]
    teams = [Team(roster_id=str(i), division_id=(1 if i <= 2 else i - 1)) for i in range(1, 13)]
    return _league(divisions=divisions, teams=teams)


#: Rosters 1 and 2 land on different cards, so each one's own tag is observable.
_SPLIT_PAIRS = [(1, 3), (2, 4), (5, 6), (7, 8), (9, 10), (11, 12)]


def test_a_division_is_clinched_only_when_no_rival_can_reach_the_current_total() -> None:
    zeros = {str(i): (0, 0, 0) for i in range(3, 13)}
    league = _two_division_league()
    # Roster 1 has 5; the rival's ceiling (3 + 2) equals it: a tiebreak could flip it.
    level = _scenario(records={"1": (5, 0, 0), "2": (3, 0, 0)} | zeros, pairs=_SPLIT_PAIRS, league=league)
    assert not _sides(level)["1"].clinched_division
    assert "division_race" in _card_for(level, "1").stakes

    # A rival whose ceiling is one short (2 + 2 = 4 < 5) is out of reach.
    clear = _scenario(records={"1": (5, 0, 0), "2": (2, 0, 0)} | zeros, pairs=_SPLIT_PAIRS, league=league)
    assert _sides(clear)["1"].clinched_division
    assert "division_race" not in _card_for(clear, "1").stakes


def test_the_division_race_ends_once_the_leader_is_strictly_above_the_rival_ceiling() -> None:
    zeros = {str(i): (0, 0, 0) for i in range(3, 13)}
    league = _two_division_league()
    # Rival 2's ceiling is 5; the leader's current total (6) is strictly above it.
    over = _scenario(records={"1": (6, 0, 0), "2": (3, 0, 0)} | zeros, pairs=_SPLIT_PAIRS, league=league)
    assert "division_race" not in _card_for(over, "2").stakes

    # At exactly 5 the rival can still tie, so roster 2's race is live.
    level = _scenario(records={"1": (5, 0, 0), "2": (3, 0, 0)} | zeros, pairs=_SPLIT_PAIRS, league=league)
    assert "division_race" in _card_for(level, "2").stakes


def test_the_game_of_the_week_ranks_stakes_before_model_rank() -> None:
    result = _scenario(records=_RECORDS, pairs=_PAIRS)

    # Rosters 1 and 2 carry the best ranks but have clinched everything: no tags.
    assert _card_for(result, "1").stakes == []
    picked = result.game_of_week
    assert picked is not None and picked.rule == "most_stakes"
    marked = [card for card in result.cards if card.game_of_week]
    assert len(marked) == 1
    # Among the equally-tagged cards the lowest rank sum wins: rosters 7 v 8.
    assert {marked[0].a.roster_id, marked[0].b.roster_id} == {"7", "8"}
    assert picked.rank_sum == 15


def test_equal_unranked_cards_fall_to_the_lowest_matchup_id() -> None:
    records = {str(i): (10, 0, 0) for i in range(1, 13)}
    ids = sorted(records, key=lambda rid: int(rid))
    week_model = WeekModel(
        week=9,
        rosters=[Roster(roster_id=roster_id) for roster_id in ids],
        matchups=[],
        transactions=[],
        playoff_week_start=10,
        next_matchups=_pair_rows(_PAIRS, 10),
    )
    unranked = PowerRanks(
        week=9,
        teams=[
            TeamPower(
                roster_id=roster_id,
                model_score=None,
                model_rank=None,
                all_play_pct=None,
                normalized_avg_pf=None,
                win_pct=None,
            )
            for roster_id in ids
        ],
    )
    result = compute_next_week(
        week_model, _league(), _standings(records, week=9, through=9), unranked, {}, None
    )

    assert result.game_of_week is not None
    assert result.game_of_week.matchup_id == 1
    assert result.game_of_week.rank_sum is None
    assert result.game_of_week.rule == "top_ranks"
    assert all(card.a.model_rank is None and card.b.model_rank is None for card in result.cards)


def test_a_league_with_no_playoff_week_start_stands_every_flag_and_tag_down() -> None:
    result = _scenario(records=_RECORDS, pairs=_PAIRS, playoff_week_start=None)

    assert len(result.cards) == len(_PAIRS)
    for card in result.cards:
        assert card.stakes == []
        for side in (card.a, card.b):
            assert not (
                side.clinched_playoff or side.clinched_bye or side.eliminated or side.clinched_division
            )


def test_lone_null_and_three_roster_groups_never_become_cards() -> None:
    extra = [
        Matchup(week=8, roster_id="7", matchup_id=60),
        Matchup(week=8, roster_id="8", matchup_id=60),
        Matchup(week=8, roster_id="9", matchup_id=60),  # a group of three
        Matchup(week=8, roster_id="10", matchup_id=61),  # a lone row: a bye
        Matchup(week=8, roster_id="11", matchup_id=None),
        Matchup(week=8, roster_id="12", matchup_id=None),
    ]
    result = _scenario(records=_RECORDS, pairs=[(1, 2), (3, 4), (5, 6)], extra_next=extra)

    assert [(card.a.roster_id, card.b.roster_id) for card in result.cards] == [
        ("1", "2"),
        ("3", "4"),
        ("5", "6"),
    ]


def test_a_different_bracket_size_moves_the_lines() -> None:
    records = {str(i): (10, 0, 0) for i in range(1, 5)} | {str(i): (0, 0, 0) for i in range(5, 13)}

    four = _sides(_scenario(records=records, pairs=_PAIRS, league=_league(playoff_teams=4)))
    # Four in, no byes: roster 5's ceiling is 2 and four rosters are strictly above it.
    assert four["5"].eliminated
    assert four["1"].clinched_playoff and not four["1"].clinched_bye

    six = _sides(_scenario(records=records, pairs=_PAIRS, league=_league(playoff_teams=6)))
    assert not six["5"].eliminated
