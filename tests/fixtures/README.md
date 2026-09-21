# Test fixtures

Each `*.json` file here is one **self-contained, anonymized Sleeper league
bundle** — everything the engine needs for one scenario, in a single object, with
no network access. CI and every offline test run against these. Each is under
**450 KB**.

**Minified.** Fixtures are compact JSON (no `indent=2`), not pretty-printed.
Adding `players_points`/`starters_points` to every matchup row (below) pushed
`week10-blowout.json` well past the old pretty-printed size; dropping the
indent (the size-ceiling decision — see `tools/anonymize.py`'s final
`json.dump`) shrank it from 422 KB to ~235 KB, leaving headroom for per-player
points and the wider playoff-window fixture with no budget change. Pipe a
fixture through `python -m json.tool` (or your editor's JSON formatter) to
read it. To review a *diff* to one meaningfully, use `git diff --word-diff`
or pretty-print both revisions before comparing — a minified file otherwise
renders as one full-line replacement.

## The bundle shape

```jsonc
{
  "meta":     { "case": "week10-blowout", "target_week": 10, "exercises": "…" },
  "league":   { /* name + ids anonymized; scoring_settings, roster_positions, settings verbatim */ },
  "users":    [ /* display_name + team_name from the generated pool; user_id tokenized; avatar dropped */ ],
  "rosters":  [ /* owner_id / co_owners tokenized; players / starters / settings verbatim */ ],
  "matchups": { "1": [ /* points, players, starters, matchup_id, players_points, starters_points */ ], "…": [] },
  "transactions": { "1": [ /* settled transactions only; ids tokenized; metadata dropped;
                             created / status_updated remapped onto a synthetic grid */ ], "…": [] },
  "draft":        { /* ids tokenized; settings verbatim; metadata → name + scoring_type only */ },
  "draft_picks":  [ /* NFL player names kept */ ],
  "traded_picks": [ /* roster-id refs only */ ],
  "winners_bracket": [], "losers_bracket": [],
  "players": { "<player_id>": { "first_name": "", "last_name": "", "position": "",
                                "team": "", "years_exp": 0, "number": "",
                                "injury_status": "", "fantasy_positions": [], "status": "",
                                "college": "", "age": 0, "rookie_year": "" } }
}
```

**Playoff brackets.** `winners_bracket`/`losers_bracket` are populated from the
raw export only for a fixture whose `target_week` falls in the playoff period
(`league.settings.playoff_week_start`); every other fixture emits `[]` for
both — the raw bracket files hold the *completed* season's results, which
would otherwise leak future outcomes into an earlier week's fixture.

**Timestamps.** Every `created` / `status_updated` epoch-ms is replaced,
order-preserving, by a point on a synthetic grid (`2025-01-01T00:00:00Z` +
`rank × 1 h` over the sorted distinct set). Relative ordering survives; the real
wall-clock — a strong fingerprint of a private league — does not.

**Player public facts.** `college` / `age` / `rookie_year` are kept (published on
every NFL roster site); `birth_date` / `birth_city` / `high_school` and every
third-party id are dropped. `rookie_year` is lifted out of the raw record's
`metadata` sub-object.

**`rosters` is one season-final pull, not a weekly snapshot.** Every fixture's
`rosters` section carries the *season-end* record, points-for and points-against
— a mid-season slice has week-10 matchups next to week-14 totals. Story 5.6's
`stats/standings.py` therefore folds `matchups` instead of reading those fields,
and uses them only to cross-check the fold (`cross_check_standings`). That is why
a weekly run against a committed mid-season fixture trips the cross-check by
design, and why `week17-playoffs.json` is the fixture whose cross-check passes:
its regular-season fold matches W-L-T exactly and points-for within
`POINTS_FOR_ROUNDING_TOLERANCE × folded weeks` (`0.005` per week — the half-cent
each week's 2-dp points can drift; the real week-17 drift is exactly
`14 × 0.005`).

`rookie-draft.json` is the pre-week-1 state: `meta.target_week` is `null` and
`matchups` / `transactions` / brackets are empty. It is the fixture the Epic 2
draft recap is built against.

## The fixtures

| File | Scenario |
|------|----------|
| `rookie-draft.json` | Keeper/dynasty rookie draft, post-draft rosters, traded picks. Pre-week-1. |
| `week01-openers.json` | The week-1 season opener. No prior-week history — `matchups`/`transactions` hold only week 1. |
| `week02-nailbiter.json` | A sub-1-point top game (both teams over 200), plus a second close game the same week. Weeks 1–2. |
| `week05-trade.json` | A lopsided "sell the vet" dynasty trade with 2026/2027 pick swaps, heavy FAAB, a season-high score, and an ~84-point blowout. Weeks 1–5. |
| `week08-median.json` | A synthetic median-scoring week: curated `custom_points` overrides so two rosters tie exactly at the week-8 league median, five score strictly below, five strictly above. Weeks 1–8. |
| `week10-blowout.json` | Three blowouts (loser under 65% of the winner). This is the reference-newsletter week. Weeks 1–10. |
| `week10-superflex.json` | The week-10 bundle with the second `QB` roster slot changed to `SUPER_FLEX` (a roster-slot property; scoring is unchanged), then run through the anonymizer. A synthetic superflex league to exercise the optimal-lineup solver. |
| `week17-playoffs.json` | The championship round (`playoff_week_start` is 15). Non-empty `winners_bracket`/`losers_bracket`; four rosters eliminated in an earlier round have no matchup entry this week. Weeks 1–17. |

`week10-superflex.json` is the fixture the Story 5.5 optimal-lineup solver is
exercised end to end against: its `SUPER_FLEX` slot must accept a second `QB`,
which `tests/test_stats_lineup.py` asserts directly on the committed bundle. `week10-blowout`
and `week10-superflex` are the two fixtures that carry per-player
`players_points`/`starters_points` for *every* week (Story 5.2), which is what makes a
hand-checkable lineup optimum possible at all.

`week10-blowout.json` is the fixture Story 5.6's standings and power rank are
pinned against (its ranks, divisions and derived playoff picture are copied
verbatim into `tests/test_stats_standings.py`), and it is the fixture whose
cross-check raises — its season-final `rosters` cannot match a week-10 fold.
`week17-playoffs.json` is the fixture Story 5.6's regular-season freeze and
passing cross-check are pinned against.

## Provenance and regeneration

All eight derive from one private Phase-0 pull of a real 12-team Sleeper league
(0.5 PPR, TE premium, 2-QB, keeper/dynasty, `playoff_week_start: 15`,
`playoff_teams: 6`). **The raw source is private** and lives outside this
repository (`../brief/phase-0/raw/`, gitignored). It is never committed — these
anonymized bundles are the only league data in the repo.

Regeneration is two committed tools. First
[`tools/assemble_bundle.py`](../../tools/assemble_bundle.py) reads the
per-endpoint files in `../brief/phase-0/raw/` and builds one bundle for a named
case — truncating the week window, dropping non-settled transactions, applying
the `QB → SUPER_FLEX` slot change for `week10-superflex`, populating
`winners_bracket`/`losers_bracket` for a case whose `target_week` reaches the
playoff period (`week17-playoffs`), applying each case's curated matchup
mutation (the eliminated-roster drop for `week17-playoffs`, the `custom_points`
override for `week08-median`), and attaching `meta`. Then
[`tools/anonymize.py`](../../tools/anonymize.py) strips every section (and
every `metadata` sub-object) to an allowlist, replaces member/team names from a
bundled pool, rewrites every Sleeper account/league/draft id to an opaque `id_…`
token, remaps timestamps onto the synthetic grid, and drops avatar hashes and
URLs:

```bash
uv run python tools/assemble_bundle.py ../brief/phase-0/raw <case> \
  | uv run python tools/anonymize.py - --seed 0 > tests/fixtures/<case>.json
```

`--seed` is optional (default `0`). `<case>` is one of: `rookie-draft`,
`week01-openers`, `week02-nailbiter`, `week05-trade`, `week08-median`,
`week10-blowout`, `week10-superflex`, `week17-playoffs`.

Both tools are **deterministic**: at `--seed 0` the pipeline reproduces each
committed fixture byte-for-byte, and
`tests/test_fixtures.py::test_committed_fixture_reproduces_byte_for_byte_from_raw`
asserts exactly that wherever the private raw source is present. `test_fixtures.py`
also asserts, positively, that no real name, id, avatar, or wall-clock timestamp
survives in any committed fixture.

## Contributing a fixture

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md). Assemble your own league export
into the bundle shape above, run it through `tools/anonymize.py`, and open a PR
with a one-line note on what it breaks. Never commit a raw, un-anonymized export.
