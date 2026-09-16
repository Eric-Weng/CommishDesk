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
