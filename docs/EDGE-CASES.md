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
