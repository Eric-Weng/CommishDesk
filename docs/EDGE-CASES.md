# Known edge cases

Documented limitations that are accepted behavior, not bugs — each one traded off
deliberately rather than left as an unnoticed gap. New entries land here as later
stories (5.5, 5.7, …) hit their own edge cases; this file is a point of reference for
those, not a place to record a bug that should simply be fixed.

## Backfill: a week generated long after the fact records the *current* team, not the
## historical one (Story 5.3b, AC5)

**What happens.** `ingest/build.py::get_player_snapshot` persists a `PlayerSnapshot` map
the first time a league-week is generated, and every later regeneration of that same
week reuses the persisted snapshot verbatim (FR-5) — a trade made after week `n` shipped
must never silently rewrite week `n`'s facts.

That guarantee only starts once a snapshot exists. If week `n` is generated for the
first time long after week `n` actually happened — a backfill, or a league onboarding
mid-season — there is no persisted snapshot yet to reuse, so `get_player_snapshot` builds
one from the bundle `fetch_week` returns *today*. That bundle's `"players"` key reflects
*today's* NFL rosters, not week `n`'s. A player who was on Team A in week `n` but has
since been traded to Team B is recorded as Team B.

**Why this is accepted, not fixed.** Nothing in the Sleeper API (or this engine) tracks
an NFL player's team *as of* an arbitrary past week — only draft-pick metadata (frozen at
draft time) and the live `/players/nfl` table (today's snapshot) exist. Reconstructing a
true historical team/position would need a third, dated data source this story does not
have. The bug this story *does* fix — a past week silently changing on every
regeneration — is still fixed: once the first snapshot is taken (even a backfilled one),
it is frozen from then on.

**Where this shows up.** A backfilled week's box score or recap may cite a player's
*current* team instead of the team they played for that week. This is most visible for a
player traded mid-season into a league that backfills its early weeks afterward.

## Availability is "whoever Sleeper scored", whatever the injury tag says (Story 5.5)

**What happens.** `stats/lineup.py::compute_weekly_lineups` builds each roster's optimal
pool from the players Sleeper published a week score for. An injury designation never
removes a player: if Sleeper scored him on that roster that week, he is placeable, even
with an `IR`/`Out`/`Questionable` tag. A player who was ruled out before kickoff and
never played is simply *not scored*, so he naturally contributes nothing — the solver
fills the slot from whoever is left.

The pool does drop `Roster.ir` occupants — **except the ones who actually started that
week**. An IR slot is not startable, but the fixtures' IR list is *season-end* state, not
week-N state (five week-10 rosters started a player who is on it), so a strict exclusion
would remove a starter and report coaching efficiency above 100 %. `Roster.taxi`
occupants are **not** dropped: Sleeper lets a taxi player be activated to the bench at
any time (taxi is a second bench), so he is available. Activating one needs an open
roster spot, which the solver does not model.

**Why this is accepted, not fixed.** Sleeper's own weekly points are the league's ground
truth (PRD §17 Q6); re-deriving availability from depth charts, injury reports, or
kickoff times would need dated data sources this engine does not have.

**Divergence from the phase-0 golden.** Excluding IR and taxi together (the first cut of
this story) reconciled only six of twelve rosters; excluding IR only reconciles eleven.
The one that remains is roster 4: the private `brief/phase-0/week10-facts.json` golden
counted a non-starting player who is on the season-end IR list, so its `optimal` (181.22)
is higher than this module's (178.37). Per the project's golden-file rule, that divergence
is recorded here rather than bent to match: `tests/test_stats_lineup.py` reconciles the
other eleven exactly and asserts roster 4 is at or below its golden value on `optimal`,
`points_left_on_bench`, and `bench_regret`.

**Where this shows up.** A week-10 (or later) recap built from a season-end backfill can
show a slightly lower `optimal` and `points_left_on_bench` for a roster whose IR stash
scored but never started. A live Wednesday run reads current IR state, so this is mainly
a backfill and fixture limitation; Sleeper has no historical per-week IR view.

## A player added after his game kicked off can still be scored for it (Story 5.5)

**What happens.** A manager can add a player whose NFL game has *already finished* — a
waiver claim or free-agent add processed after kickoff. Sleeper will then score that
player's already-played game onto the roster, and `stats/lineup.py` has no way to tell
that apart from a legitimate pre-kickoff start: the points landed, so the pool sees them.

**Why this is accepted, not fixed.** Nothing the ingest layer carries can date the
addition. `ingest/model.py::Transaction` records the settled assets and roster ids but
**no timestamp** (`created` / `status_updated` are on the raw Sleeper payload; the model
deliberately does not carry them), and the bundle carries no NFL kickoff schedule at all.
Detecting a too-late add would need both a dated transaction log and per-game kickoff
times — a second and third data source this story does not have.

**Where this shows up.** In a league whose waivers clear after Sunday's early games, a
roster may show an "optimal" lineup that includes a player the manager could not legally
have started. The overstatement is bounded by the late-added players' scores and only ever affects
that week's lineup stats, never the record or the standings.

## Standings are folded from matchups; Sleeper's roster totals are season-final (Story 5.6)

**What happens.** `stats/standings.py::compute_standings` derives every standing —
W-L-T, points for and against, streak, high/low week — by folding `WeekModel.matchups`
over weeks `1..cutoff`. It never reads `Roster.wins` / `losses` / `ties` / `fpts`. Those
roster fields feed exactly one thing: `cross_check_standings`, which compares the fold
against Sleeper's own totals.

**Why.** Every committed fixture's `rosters` section is a single *season-final* pull, so a
mid-season slice carries week-10 matchups next to week-14 records — `week10-blowout.json`
roster 1 reads 10–4 while the week-10 fold is 7–3. Reading the roster totals would ship a
wrong table; folding the matchups reproduces the phase-0 golden exactly for all twelve
rosters (verified in `tests/test_stats_standings.py`).

**Where this shows up.** A weekly run against a committed mid-season fixture trips the
cross-check by construction (its roster totals are season-final). That is a hold, not a
wrong number: the CLI's per-league `except CommishDeskError` prints one line and moves on.
`week17-playoffs.json` is regular-season-final, so it is the fixture whose cross-check
passes — with points-for matched to within `POINTS_FOR_ROUNDING_TOLERANCE` times the
number of folded weeks (0.005 per week, the half-cent each week's 2-dp points can drift;
the real week-17 drift is exactly `14 × 0.005`).

## A median-scoring league trips the cross-check by design (Story 5.6)

**What happens.** Some Sleeper leagues set `league_average_match` (median scoring), which
awards an extra win or loss each week against the league median. Sleeper folds those into
the W-L-T it reports on `Roster`; `compute_standings` folds head-to-head matchups only, so
the two disagree by roughly one decision per week and `cross_check_standings` raises.

**Why this is accepted, not fixed.** The median result is not in `WeekModel.matchups` —
the extra decision lives only in Sleeper's own aggregate, and Sleeper's median formula is
undocumented. Guessing at it risks a wrong number; the cross-check instead fails closed,
which is the designed behaviour for "the engine and the platform disagree".

**Where this shows up.** A median-scoring league cannot ship a weekly Issue until a later
story models the median result. The standings module itself is unchanged — the league
simply holds.
`stats/stakes.py` (Story 5.7) inherits the limit: it assumes one decision per week when it
sizes a roster's ceiling, but a median league is held by the cross-check before a preview
can ship, so it never states a wrong "clinched" or "eliminated".

## The standings tiebreak is named, never implicit (Story 5.6)

Win percentage descending, then points-for descending, then `roster_id` (numeric where
parseable). The key consulted after win percentage is exposed as
`stats/standings.py::TIEBREAK` and surfaced as `Standings.tiebreak`, so a reader — or a
later story — never has to guess which one decided a rank. Division order uses the same
key, restricted to that division's rosters.

## The next-week schedule exists only inside the regular season (Story 5.7)

**What happens.** `SleeperAdapter.fetch_week` fills the bundle's `next_matchups` key with
week `n + 1`'s pairings only while `n + 1` is still regular season (the league's
`settings.playoff_week_start` is an int and `n + 1 < playoff_week_start`). At the last
regular-season week and every week after, `next_matchups` is `[]` and no request is made.
`stats/stakes.py` mirrors that: once the previewed week `n + 1` reaches `playoff_week_start`
(i.e. `n` is the last regular-season week), every card's `stakes` is `[]` even if a caller
hand-supplies pairings. Only the tags stand down; the clinch and elimination flags stay
computed, since with no games left they are the final regular-season picture.

**Why this is accepted, not fixed.** The pairings are projected to `{roster_id,
matchup_id}` only — never a score, a lineup or a player list — so a fixture can never leak
a future outcome. Past the cutoff there is no regular-season pairing to project (Sleeper
reuses the matchup endpoints for bracket games), and playoff-week framing is a later
story's (5.15 / 5.16).

**Where this shows up.** A week-14 run (`playoff_week_start` 15) writes no
`matchups.next_week` cards, and a week-17 run has none either. The transactions desk is
unaffected — the market note still spans the whole season.

## Week 1's transaction bucket also holds the offseason (Story 5.7)

**What happens.** Sleeper files a league's offseason trades (and every other settled move
made before the season started) under week 1's transactions endpoint. A week-1 run's
`this_week` can therefore list moves made months earlier, and
`market_note.weeks_since_last_trade` can be large while still being honest about the
league's last trade.

**Why this is accepted, not fixed.** Sleeper buckets a move by week and nothing else;
there is no separate "offseason" bucket to read. `ingest/model.py::Transaction` carries no
timestamp either (`created` / `status_updated` are on the raw payload), so the engine
could not re-bucket a move even if it wanted to.

**Where this shows up.** A week-1 or early-season desk may attribute an offseason trade to
week 1. Every later week's history is unaffected: `past_transactions` keys each move by
the week the bundle carries it under.

## Clinch and elimination are conservative (Story 5.7)

**What happens.** `stats/stakes.py` decides every clinch/elimination claim in win
equivalents, and only ever claims what a tiebreak cannot flip. Elimination needs `N`
others *strictly* above a roster's maximum; a playoff berth needs at most `N - 1` others
able to reach its current total. A roster exactly level with `N` others on its ceiling is
therefore reported as neither clinched nor eliminated.

**Why this is accepted, not fixed.** A tiebreak (points-for, then roster id) can decide a
real berth, and this module has no view into it. Erring toward "the race is live" is the
safe direction: a reader is never told a race is over when it is not. Concretely, roster
7 of a hand-built twelve-team league whose ceiling equals the six leaders' current total
is `eliminated=False` — the six are not *strictly* above it.

**Where this shows up.** A genuinely-clinched roster can still carry a `wildcard_race`
tag, and a genuinely-eliminated one can carry an `elimination` tag, in the narrow band
where a tiebreak would decide it. The flags are conservative on purpose.

## No divisions, no playoff format: the stakes stand down (Story 5.7)

**What happens.** A league whose `format.divisions` is empty never emits `division_race`
and never reports `clinched_division`. A league whose `league.format.playoff` is `None`
stands the playoff flags down (all `False`) and emits no playoff tag, because `N` is
undefined. And a league whose `playoff_week_start` is `None`, or whose previewed week is
at or past `playoff_week_start`, stands *every* stake down — every card's `stakes` is
`[]`, with the cards, ranks, records and one game of the week still emitted.

**Why this is accepted, not fixed.** Divisions and the playoff bracket shape are
league-as-data (`LeagueFormat`), and a league that declares neither has no race to model.
Playoff-week framing is a later story's (5.15 / 5.16), so this one deliberately frames
nothing rather than guessing.

**Where this shows up.** A week-14 run (`playoff_week_start` 15) has no cards at all, because
the adapter fetched no pairings; pairings supplied by hand get `stakes == []`, which is the
right answer for the last regular-season week. A league with no `playoff_week_start` gets
every flag `False` as well.
