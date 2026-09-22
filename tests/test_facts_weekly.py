"""Story 5.8: the weekly Facts document (``build_weekly_facts``).

One test per row of the spec's I/O & Edge-Case Matrix, mirroring
``tests/test_stats_stakes.py``: the week-10 build on the committed fixture,
golden/pinned reconciliation (the private ``week10-facts.json`` is skip-gated,
the always-on pins run everywhere), cold start, a bye/eliminated roster, a
bracket week, missing bye data, the 32-team narration cap, the unreducible
error, determinism, the schema-violation path, and the AST import-fence pair
plus the "no hardcoded league shape" guard.

The committed CI oracle ``tests/fixtures/facts/expected-weekly-facts-week10.json``
is a frozen ``model_dump(mode="json")`` snapshot of the whole document, built
with a fixed ``generated_at``; the byte-equality check is skipped when the file
is absent (a workspace that predates the regeneration).

``season.luck`` does **not** reconcile with the golden's ``season.luck`` —
inherited from ``stats/weekly.py``'s own documented divergence (see
``tests/test_stats_weekly.py``'s module docstring): the fixture's ``Roster``
record is a single point-in-time pull, not a true "as of week 10" snapshot, so
the win-equivalents ``luck`` is measured against never match a live week-10
pull's. This is expected, not a bug, and is not asserted here.

``season.coaching_efficiency.pct`` also does not reconcile exactly — this
story sums ``compute_weekly_lineups`` over weeks ``1..n`` under one supplied
(week-n) player snapshot, so ``docs/EDGE-CASES.md``'s roster-4 IR/taxi
divergence (Story 5.5, week 10 only) compounds across every roster's ten
weeks. Not asserted here either; see that doc's "Divergence from the phase-0
golden" section.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from commishdesk.errors import CommishDeskError, SchemaValidationError
from commishdesk.facts import SCHEMA_VERSION, WeeklyFacts, build_weekly_facts
from commishdesk.ingest import (
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
    build_player_names,
    build_player_snapshot,
    build_week_model,
)
from tests.conftest import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FACTS_DIR = FIXTURE_DIR / "facts"
EXPECTED_WEEKLY_PATH = FACTS_DIR / "expected-weekly-facts-week10.json"
STATS_WEEKLY = REPO_ROOT / "commishdesk" / "facts" / "weekly.py"

#: The private phase-0 golden — a planning artifact outside the repo.
_GOLDEN_PATH = REPO_ROOT.parent / "brief" / "phase-0" / "week10-facts.json"
GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.is_file() else None
requires_golden = pytest.mark.skipif(
    GOLDEN is None,
    reason="phase-0 golden is a private planning artifact, not in the tree",
)
requires_committed_oracle = pytest.mark.skipif(
    not EXPECTED_WEEKLY_PATH.is_file(),
    reason="expected-weekly-facts-week10.json has not been regenerated in this workspace",
)

GENERATED_AT = "2026-09-07T00:00:00Z"
WEEK10 = "week10-blowout.json"
BYES_NEXT_WEEK = frozenset({"IND", "NO"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bundle(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _week10_facts(*, nfl_byes_next_week: frozenset[str] | None = BYES_NEXT_WEEK) -> WeeklyFacts:
    bundle = _bundle(WEEK10)
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    players = build_player_snapshot(bundle)
    names = build_player_names(bundle)
    return build_weekly_facts(
        week,
        league,
        players,
        names,
        generated_at=GENERATED_AT,
        nfl_byes_next_week=nfl_byes_next_week,
    )


def _synthetic(
    n_teams: int, *, week: int = 5, playoff_week_start: int | None = 10
) -> tuple[WeekModel, LeagueModel, dict[str, PlayerSnapshot], dict[str, str]]:
    """A hand-built N-team league-week — the 32-team cap row and the cold-start
    row share it."""
    ids = [str(i) for i in range(1, n_teams + 1)]
    league = LeagueModel(
        league_id="id_league",
        name="Big League",
        season=2025,
        platform="sleeper",
        format=LeagueFormat(
            team_count=n_teams,
            roster_slots=["QB", "RB", "WR", "TE", "FLEX"],
            flex_eligibility={"FLEX": ["RB", "WR", "TE"]},
            scoring_label="PPR",
            is_superflex_or_2qb=False,
            te_premium=False,
            playoff=PlayoffFormat(bracket_teams=min(8, n_teams)),
        ),
        teams=[Team(roster_id=r, manager=f"m{r}") for r in ids],
        picks=[],
        draft=Draft(id="id_draft"),
    )
    matchups: list[Matchup] = []
    for wk in range(1, week + 1):
        for index in range(0, n_teams, 2):
            a, b = ids[index], ids[index + 1]
            pair = index // 2 + 1
            matchups.append(
                Matchup(
                    week=wk,
                    roster_id=a,
                    matchup_id=pair,
                    opponent_roster_id=b,
                    points=100.0,
                    starters=["p1"],
                    starters_points=[100.0],
                    players_points={"p1": 100.0},
                )
            )
            matchups.append(
                Matchup(
                    week=wk,
                    roster_id=b,
                    matchup_id=pair,
                    opponent_roster_id=a,
                    points=90.0,
                    starters=["p2"],
                    starters_points=[90.0],
                    players_points={"p2": 90.0},
                )
            )
    week_model = WeekModel(
        week=week,
        rosters=[Roster(roster_id=r) for r in ids],
        matchups=matchups,
        transactions=[],
        playoff_week_start=playoff_week_start,
    )
    players = {
        "p1": PlayerSnapshot(player_id="p1", position="QB", nfl_team="KC"),
        "p2": PlayerSnapshot(player_id="p2", position="RB", nfl_team="BUF"),
    }
    names = {"p1": "Player One", "p2": "Player Two"}
    return week_model, league, players, names


# --------------------------------------------------------------------------- #
# Matrix: week-10 build
# --------------------------------------------------------------------------- #


def test_week10_build_validates_and_has_twelve_teams() -> None:
    doc = _week10_facts()
    assert isinstance(doc, WeeklyFacts)
    assert doc.schema_version == "0.5.0" == SCHEMA_VERSION
    assert doc.issue_type == "weekly"
    assert doc.week == 10
    assert len(doc.teams) == 12
    # every roster id is a string and the block is in numeric roster order
    assert [t.roster_id for t in doc.teams] == [str(i) for i in range(1, 13)]


def test_week10_has_six_this_week_and_six_next_week_games() -> None:
    doc = _week10_facts()
    assert len(doc.matchups.this_week) == 6
    assert len(doc.matchups.next_week) == 6
    for game in doc.matchups.this_week:
        assert int(game.home_roster_id) < int(game.away_roster_id)  # home is the lower id
        assert len(game.headline_players) == 2  # each side's top starter
    # The fixture's own name promises both values are reachable, not just False.
    by_pair = {(g.home_roster_id, g.away_roster_id): g.is_blowout for g in doc.matchups.this_week}
    assert by_pair[("1", "5")] is True  # margin 138.19, the golden's biggest blowout
    assert by_pair[("7", "12")] is False  # margin 6.06, the golden's closest game


def test_week10_marquee_is_three_versus_nine() -> None:
    """The spec's matrix pins the week-10 marquee as rosters 3 v 9 (both with a
    first-round bye still in reach)."""
    doc = _week10_facts()
    card = next(card for card in doc.matchups.next_week if card.game_of_week)
    assert {card.a_roster_id, card.b_roster_id} == {"3", "9"}


def test_week10_carries_the_week_nine_trade_and_the_week_ten_waiver() -> None:
    doc = _week10_facts()
    assert doc.transactions.market_note.last_trade_week == 9
    assert doc.transactions.recent_trades  # the week-9 trade is inside the window
    assert doc.transactions.this_week  # the week-10 waiver
    faabs = [move.faab for move in doc.transactions.this_week if move.faab is not None]
    assert 25 in faabs


def test_week10_teams_carry_this_week_and_next_week() -> None:
    doc = _week10_facts()
    for team in doc.teams:
        assert team.this_week is not None
        assert team.next_week is not None
        assert team.history.weekly  # ten weeks played
        assert team.season.record.w + team.season.record.l + team.season.record.t == 10


def test_this_week_starters_are_the_actual_lineup_not_the_optimal_one() -> None:
    """``starters`` must be what the manager actually started (``Matchup.starters``),
    never ``TeamLineup``'s optimal solve — the two differ whenever a roster has a
    ``bench_regret``, and a player cannot be both a starter and the bench_regret pick."""
    doc = _week10_facts()
    for team in doc.teams:
        tw = team.this_week
        assert tw is not None
        started_ids = {s.player_id for s in tw.starters if s.player_id is not None}
        assert sum(s.points for s in tw.starters) == pytest.approx(tw.points, abs=0.01)
        if tw.bench_regret is not None:
            assert tw.bench_regret.player_id not in started_ids


def test_week10_history_rows_cover_every_week_played() -> None:
    doc = _week10_facts()
    for team in doc.teams:
        assert [row.week for row in team.history.weekly] == list(range(1, 11))
        assert team.history.weekly[0].power_rank is None  # cold start
        assert team.history.weekly[0].all_play is None
        assert team.history.weekly[0].luck is None
        assert team.history.weekly[-1].power_rank is not None


def test_week10_bye_impact_names_the_starter_on_a_next_week_bye() -> None:
    doc = _week10_facts()
    flagged = [entry for card in doc.matchups.next_week for entry in (card.bye_impact or [])]
    assert flagged, "the week-10 fixture must start at least one player on a week-11 bye"
    assert {entry.nfl_team for entry in flagged} <= BYES_NEXT_WEEK
    for entry in flagged:
        assert entry.name  # names are resolved


def test_week10_era_started_on_bye_is_none_without_bye_data() -> None:
    doc = _week10_facts(nfl_byes_next_week=None)
    assert doc.period.nfl_byes_next_week is None
    for team in doc.teams:
        assert team.this_week is not None
        assert team.this_week.started_on_bye is None
        if team.next_week is not None:
            assert team.next_week.starters_on_bye == []
    for card in doc.matchups.next_week:
        assert card.bye_impact is None


def test_week10_playoff_picture_is_derived_for_the_league() -> None:
    doc = _week10_facts()
    picture = doc.standings.playoff_picture
    assert picture is not None
    assert picture.cut_line_after_rank == 6
    assert len(picture.in_bracket) == 6
    assert len(picture.byes) == 2
    assert doc.standings.overall  # ranked order
    assert doc.standings.through_week == 10


def test_week10_leaders_are_present_and_named() -> None:
    doc = _week10_facts()
    assert doc.leaders.week_high_player is not None
    assert doc.leaders.week_high_player.name
    assert len(doc.leaders.top_players) <= 5
    assert len(doc.leaders.worst_starters) <= 5
    assert doc.leaders.best_coaching is not None
    assert doc.leaders.worst_coaching is not None


# --------------------------------------------------------------------------- #
# Matrix: committed oracle (byte-equal) — skipped when absent
# --------------------------------------------------------------------------- #


@requires_committed_oracle
def test_week10_matches_the_committed_oracle() -> None:
    """The whole built document against the committed snapshot, with the fixed
    ``generated_at``."""
    built = _week10_facts().model_dump(mode="json")
    expected = json.loads(EXPECTED_WEEKLY_PATH.read_text(encoding="utf-8"))
    expected = {k: v for k, v in expected.items() if not k.startswith("_")}
    assert built == expected


# --------------------------------------------------------------------------- #
# Matrix: golden reconcile (private, skip-gated) + always-on pins
# --------------------------------------------------------------------------- #

#: The raw week-10 picture the spec's golden pins (record, rank, points-for,
#: model rank) — copied literals so the always-on suite pins the matrix without
#: reading the private file, exactly like ``tests/test_stats_stakes.py``.
_WEEK10_PINNED = {
    "1": (7, 3, 0),
    "2": (9, 1, 0),
    "3": (7, 3, 0),
    "4": (4, 6, 0),
    "5": (6, 4, 0),
    "6": (5, 5, 0),
    "7": (2, 8, 0),
    "8": (4, 6, 0),
    "9": (6, 4, 0),
    "10": (3, 7, 0),
    "11": (2, 8, 0),
    "12": (5, 5, 0),
}


def test_week10_records_match_the_pinned_golden_sides() -> None:
    doc = _week10_facts()
    by_roster = {t.roster_id: t for t in doc.teams}
    for roster_id, (w, losses, t) in _WEEK10_PINNED.items():
        record = by_roster[roster_id].season.record
        assert (record.w, record.l, record.t) == (w, losses, t), roster_id


@requires_golden
def test_week10_reconciles_with_the_phase0_golden() -> None:
    """By roster: record, rank, points for/against, all-play, model rank,
    this-week result and margin equal the golden's — or the row is pinned in
    ``docs/EDGE-CASES.md`` with the raw value and reason. ``season.luck`` and
    ``season.coaching_efficiency.pct`` are excluded — see the module
    docstring."""
    assert GOLDEN is not None
    doc = _week10_facts().model_dump(mode="json")
    golden_by_roster = {str(t["roster_id"]): t for t in GOLDEN["teams"]}
    for team in doc["teams"]:
        g = golden_by_roster[team["roster_id"]]
        assert team["season"]["record"] == g["season"]["record"]
        assert team["season"]["rank"] == g["season"]["rank"]
        assert team["season"]["points_for"] == pytest.approx(g["season"]["points_for"], abs=0.01)
        assert team["season"]["points_against"] == pytest.approx(g["season"]["points_against"], abs=0.01)
        assert team["season"]["power"]["model_rank"] == g["season"]["power"]["rank"]
        built_all_play = team["season"]["all_play"]
        golden_all_play = g["season"]["all_play"]
        assert (built_all_play["w"], built_all_play["l"]) == (golden_all_play["w"], golden_all_play["l"])
        assert built_all_play["pct"] == pytest.approx(golden_all_play["pct"], abs=0.01)
        # season.luck and season.coaching_efficiency.pct deliberately not
        # asserted here -- see the module docstring.
        if team["this_week"] is not None and g.get("this_week") is not None:
            assert team["this_week"]["result"] == g["this_week"]["result"]
            assert team["this_week"]["margin"] == pytest.approx(g["this_week"]["margin"], abs=0.01)


# --------------------------------------------------------------------------- #
# Matrix: cold start (week 1)
# --------------------------------------------------------------------------- #


def test_cold_start_week_one_carries_no_all_play_power_or_luck() -> None:
    week_model, league, players, names = _synthetic(12, week=1, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    for team in doc.teams:
        assert team.season.all_play is None
        assert team.season.expected_wins is None
        assert team.season.luck is None
        assert team.season.power.model_rank is None
        assert team.season.power.prev_model_rank is None
        assert team.season.power.week_delta is None
        assert [row.week for row in team.history.weekly] == [1]
        assert team.history.weekly[0].power_rank is None
        assert team.history.weekly[0].all_play is None
        assert team.history.weekly[0].luck is None


# --------------------------------------------------------------------------- #
# Matrix: bye / eliminated roster
# --------------------------------------------------------------------------- #


def test_a_roster_with_no_game_this_week_still_gets_a_row() -> None:
    week_model, league, players, names = _synthetic(12, week=5, playoff_week_start=10)
    # Drop roster 12's week-5 row (and its opponent's) — an eliminated roster.
    trimmed = [m for m in week_model.matchups if not (m.week == 5 and m.roster_id in {"11", "12"})]
    week_model = week_model.model_copy(update={"matchups": trimmed})
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)

    by_roster = {t.roster_id: t for t in doc.teams}
    assert by_roster["12"].this_week is None
    assert by_roster["12"].next_week is None
    assert by_roster["12"].season.rank >= 1  # still in the standings
    assert "12" in doc.standings.overall


# --------------------------------------------------------------------------- #
# Matrix: bracket week
# --------------------------------------------------------------------------- #


def test_a_bracket_week_has_no_next_week_cards() -> None:
    bundle = _bundle("week17-playoffs.json")
    week = build_week_model(bundle)
    league = build_league_model(bundle)
    doc = build_weekly_facts(
        week,
        league,
        build_player_snapshot(bundle),
        build_player_names(bundle),
        generated_at=GENERATED_AT,
    )
    assert doc.matchups.next_week == []
    assert doc.period.type == "playoff"
    for team in doc.teams:
        assert team.next_week is None


# --------------------------------------------------------------------------- #
# Matrix: 32-team narration cap
# --------------------------------------------------------------------------- #


def test_a_32_team_league_keeps_the_narration_under_the_cap() -> None:
    from commishdesk.facts.schema import NARRATION_TOKEN_CAP

    week_model, league, players, names = _synthetic(32, week=5, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert len(doc.narration.model_dump_json()) <= NARRATION_TOKEN_CAP
    # the ladder must not have fired for a normal league
    assert doc.narration.luck != []
    assert doc.narration.power != []
    assert doc.narration.standings != []
    assert doc.narration.games != []


# --------------------------------------------------------------------------- #
# Matrix: unreducible
# --------------------------------------------------------------------------- #


def test_the_weekly_ladder_raises_when_it_cannot_reduce(monkeypatch) -> None:
    from commishdesk.facts import build as build_mod

    monkeypatch.setattr(build_mod, "NARRATION_TOKEN_CAP", 50)
    week_model, league, players, names = _synthetic(12, week=5, playoff_week_start=10)
    with pytest.raises(SchemaValidationError) as excinfo:
        build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert "NARRATION_TOKEN_CAP" in str(excinfo.value)


def test_the_ladder_drops_luck_then_the_power_block(monkeypatch) -> None:
    """Tier 1 drops ``luck[]`` and every power row's ``all_play``/``luck``;
    tier 3 drops ``power[]``. ``standings`` and ``games`` survive all of it."""
    from commishdesk.facts import build as build_mod

    full = _week10_facts()
    # A cap that forces every tier.
    monkeypatch.setattr(build_mod, "NARRATION_TOKEN_CAP", len(full.narration.model_dump_json()) - 1)
    trimmed = _week10_facts()

    assert trimmed.narration.standings == full.narration.standings
    assert trimmed.narration.games == full.narration.games
    assert trimmed.narration.lead_candidates == full.narration.lead_candidates
    assert len(trimmed.narration.model_dump_json()) < len(full.narration.model_dump_json())


# --------------------------------------------------------------------------- #
# Matrix: determinism
# --------------------------------------------------------------------------- #


def test_determinism_equal_model_dump() -> None:
    first = _week10_facts()
    second = _week10_facts()
    assert first.model_dump() == second.model_dump()
    assert first.model_dump_json() == second.model_dump_json()


# --------------------------------------------------------------------------- #
# Matrix: schema violation
# --------------------------------------------------------------------------- #


def test_a_missing_roster_stage_result_raises_a_typed_error(monkeypatch) -> None:
    """A malformed stage result inside the merge becomes a chained
    ``SchemaValidationError`` naming ``weekly``."""
    from commishdesk.facts import weekly as weekly_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise KeyError("standings row missing")

    monkeypatch.setattr(weekly_mod, "_season_block", _boom)
    week_model, league, players, names = _synthetic(4, week=3, playoff_week_start=10)
    with pytest.raises(SchemaValidationError) as excinfo:
        build_weekly_facts(week_model, league, players, names, generated_at=GENERATED_AT)
    assert isinstance(excinfo.value, CommishDeskError)
    assert "weekly" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def test_an_unresolved_player_id_falls_back_to_the_team_then_the_id() -> None:
    week_model, league, players, names = _synthetic(4, week=3, playoff_week_start=10)
    # no name for p2 -> falls back to its snapshot nfl_team ("BUF")
    doc = build_weekly_facts(week_model, league, players, {}, generated_at=GENERATED_AT)
    names_seen = {starter.name for team in doc.teams for starter in (team.this_week.starters if team.this_week else [])}
    assert "BUF" in names_seen  # p2's team
    assert "KC" in names_seen  # p1's team


def test_a_player_missing_from_the_snapshot_falls_back_to_the_id() -> None:
    week_model, league, players, _names = _synthetic(4, week=3, playoff_week_start=10)
    doc = build_weekly_facts(week_model, league, players, {}, generated_at=GENERATED_AT)
    # every starter has *some* label (name, team, or id)
    for team in doc.teams:
        if team.this_week is None:
            continue
        for starter in team.this_week.starters:
            if starter.player_id is not None:
                assert starter.name


# --------------------------------------------------------------------------- #
# Fences
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
                names.add(f"commishdesk.facts.{node.module}" if node.module else "commishdesk.facts")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            if fname in {"import_module", "__import__"}:
                names.update(
                    a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return names


def test_weekly_never_reaches_the_network_adapters_store_or_a_later_stage() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    forbidden_roots = {"adapters", "store", "narrate", "render", "deliver", "statmods", "consensus"}
    offenders = [
        name
        for name in imported
        if name.split(".")[:1] == ["commishdesk"]
        and len(name.split(".")) > 1
        and name.split(".")[1] in forbidden_roots
    ]
    assert not offenders, offenders
    assert "commishdesk.ingest" in imported
    assert any(name.startswith("commishdesk.stats") for name in imported)
    assert "commishdesk.errors" in imported


def test_weekly_imports_no_clock_prng_or_filesystem_module() -> None:
    imported = _imported_dotted_names(STATS_WEEKLY)
    banned = {"os", "pathlib", "random", "secrets"}
    hits = {name for name in imported if name.split(".")[0] in banned}
    assert not hits, hits


def test_weekly_source_carries_no_hardcoded_league_shape() -> None:
    """AC: no team count, bracket size or slot list appears as a literal in
    ``facts/weekly.py`` — every one of them is read from the league's own
    ``format``."""
    tree = ast.parse(STATS_WEEKLY.read_text(encoding="utf-8"))
    integers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
    }
    assert 12 not in integers  # a 12-team league is the fixture default
    assert 11 not in integers  # an 11-slot starting lineup
    assert 6 not in integers  # a six-team bracket
    assert 14 not in integers  # a 14-week regular season


def test_weekly_names_are_reachable_via_the_package_re_export() -> None:
    import commishdesk.facts as facts

    assert facts.build_weekly_facts is build_weekly_facts
    assert facts.WeeklyFacts is WeeklyFacts
    for name in ("WeeklyFacts", "build_weekly_facts"):
        assert name in facts.__all__


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #


def test_tunables_are_named_module_constants() -> None:
    from commishdesk.facts import weekly

    assert weekly.TOP_PERFORMERS == 3
    assert weekly.DUD_POINTS == 6.0
    assert weekly.DUD_LIMIT == 3
    assert weekly.TOP_PLAYERS == 5
    assert weekly.WORST_STARTERS == 5


def test_generated_at_rule_is_shared_with_the_draft_recap_builder() -> None:
    from commishdesk.facts.weekly import _generated_at

    assert _generated_at("2026-09-07T00:00:00Z") == "2026-09-07T00:00:00Z"
    with pytest.raises(SchemaValidationError):
        _generated_at("")
