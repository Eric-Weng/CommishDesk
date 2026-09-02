# Test fixtures

Each `*.json` file here is one **self-contained, anonymized Sleeper league
bundle** — everything the engine needs for one scenario, in a single object, with
no network access. CI and every offline test run against these. Each is under
**400 KB**.

## The bundle shape

```jsonc
{
  "meta":     { "case": "week10-blowout", "target_week": 10, "exercises": "…" },
  "league":   { /* name + ids anonymized; scoring_settings, roster_positions, settings verbatim */ },
  "users":    [ /* display_name + team_name from the generated pool; user_id tokenized; avatar dropped */ ],
  "rosters":  [ /* owner_id / co_owners tokenized; players / starters / settings verbatim */ ],
  "matchups": { "1": [ /* points, players, starters, matchup_id */ ], "…": [] },
  "transactions": { "1": [ /* settled transactions; ids tokenized; metadata dropped */ ], "…": [] },
  "draft":        { /* ids tokenized; settings verbatim; metadata → name + scoring_type only */ },
  "draft_picks":  [ /* NFL player names kept */ ],
  "traded_picks": [ /* roster-id refs only */ ],
  "winners_bracket": [], "losers_bracket": [],
  "players": { "<player_id>": { "first_name": "", "last_name": "", "position": "",
                                "team": "", "years_exp": 0, "number": "",
                                "injury_status": "", "fantasy_positions": [], "status": "" } }
}
```

`rookie-draft.json` is the pre-week-1 state: `meta.target_week` is `null` and
`matchups` / `transactions` / brackets are empty. It is the fixture the Epic 2
draft recap is built against.

## The fixtures

| File | Scenario |
|------|----------|
| `rookie-draft.json` | Keeper/dynasty rookie draft, post-draft rosters, traded picks. Pre-week-1. |
| `week02-nailbiter.json` | A sub-1-point top game (both teams over 200), plus a second close game the same week. Weeks 1–2. |
| `week05-trade.json` | A lopsided "sell the vet" dynasty trade with 2026/2027 pick swaps, heavy FAAB, a season-high score, and an ~84-point blowout. Weeks 1–5. |
| `week10-blowout.json` | Three blowouts (loser under 65% of the winner). This is the reference-newsletter week. Weeks 1–10. |
| `week10-superflex.json` | The week-10 bundle with the second `QB` roster slot changed to `SUPER_FLEX` (a roster-slot property; scoring is unchanged), then run through the anonymizer. A synthetic superflex league to exercise the optimal-lineup solver. |

## Provenance and regeneration

All five derive from one private Phase-0 pull of a real 12-team Sleeper league
(0.5 PPR, TE premium, 2-QB, keeper/dynasty). **The raw source is private** and
lives outside this repository (`../brief/phase-0/raw/`, gitignored). It is never
committed — these anonymized bundles are the only league data in the repo.

Each was produced by piping one raw bundle through
[`tools/anonymize.py`](../../tools/anonymize.py), which strips every section (and
every `metadata` sub-object) to an allowlist, replaces member/team names from a
bundled pool, rewrites every Sleeper account/league/draft id to an opaque `id_…`
token (deterministically under `--seed`), and drops avatar hashes and URLs:

```bash
uv run python tools/anonymize.py path/to/raw-bundle.json --seed 0 > tests/fixtures/<case>.json
```

`--seed` is optional (default `0`). The tool does **not** truncate weeks, drop
failed waiver claims, or apply the `QB → SUPER_FLEX` slot change — those are
**manual pre-steps** performed on the raw bundle (by a small assembly script that
reads only the private source and is not committed) *before* it is piped through
the tool.

**Verification is `tests/test_fixtures.py`, not a re-run.** Re-running the tool on
a committed fixture produces a *different* but equally-anonymous file (tokens and
personas are remapped), so it will not diff-match. The test suite is what asserts,
positively, that no real name, id, or avatar survives in any committed fixture.

## Contributing a fixture

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md). Run your own league export through
`tools/anonymize.py` and open a PR with a one-line note on what it breaks. Never
commit a raw, un-anonymized export.
