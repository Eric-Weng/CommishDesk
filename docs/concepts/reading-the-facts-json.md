---
doc_id: reading-the-facts-json
kind: guide
title: Reading the Facts JSON
schema_min: 0.10.0
---
## What the Facts JSON is

Every issue is written from one JSON document. The engine computes every number once, and the narrator, the renderers and the safety checks all read that same file. If a number is in the newsletter, it is in the Facts JSON. This guide explains how to read the file. The stat docs explain the numbers.

There are two kinds of document. A `weekly` document describes one league-week. A `draft_recap` document describes a draft. Their shapes are published in `docs/facts-schema.json`, and each carries a `schema_version`. A key added later bumps the minor version, and readers written for an earlier minor version still load newer files.

## Naming a field

The concept docs refer to fields by their path from the document root, with `[]` marking a list. For example `weekly.teams[].season.luck` is the `luck` value inside the `season` block of each entry in the `teams` list of a weekly document, and `draft_recap.picks[].delta` is the `delta` of each pick. A doc can claim a whole block: a path covers everything beneath it.

## Reading values

- **Rounding.** Scores are stored to two decimals, ratios such as `pct` to three, and `luck` and `expected_wins` to one. Values are rounded once, from the unrounded figure.
- **Sign conventions.** `margin` is signed from the team's side, so a loss is negative. A positive `delta` is a draft value pick and a negative one a reach. A positive `week_delta` means a team moved up the power rank.
- **Ids are strings.** Roster ids are strings such as `"1"`. They sort numerically where they parse as numbers.
- **`null` has a meaning.** A `null` means the value does not exist yet or is unknown, and it is never a stand-in for zero. Week 1 has no all-play, luck or model rank. A `null` bye list means the caller gave no bye data, which is different from an empty list.
- **Frozen after the regular season.** Season totals, standings, all-play, luck and the model rank stop moving when the playoffs begin. The issue-week blocks keep following the games.
- **Ties.** Wherever a stat has a tie-break, it is written in that stat's doc. The usual order is the stat, then points for, then roster id.

## Where the blocks are explained

| Block | Doc |
| --- | --- |
| `teams[].season.record`, `rank`, `standings` | `record-and-standings` |
| `teams[].season` points, `teams[].this_week` scores | `points-and-scoring` |
| `teams[].season.all_play` | `all-play` |
| `teams[].season.expected_wins`, `luck` | `expected-wins-and-luck` |
| `teams[].season.power` | `power-rank-and-nudge` |
| `coaching_efficiency`, `bench_regret` | `coaching-efficiency-and-bench-regret` |
| `period.summary`, `matchups.this_week` | `blowout-and-week-shape` |
| `standings.playoff_picture` | `playoff-picture` |
| `matchups.next_week`, `teams[].next_week` | `stakes-and-next-week` |
| `teams[].season.streak` | `streaks` |
| `transactions` | `transactions-and-trade-market` |
| NFL bye lists | `byes` |
| `leaders` | `leaders` |
| `teams[].history` | `team-history` |
| `lead_candidates`, `storyline_candidates` | `leads-and-storylines` |
| `draft_recap.picks` | `pick-delta-and-consensus` |
| `draft_recap.teams[]` grades | `draft-grade` |
| `draft_recap.teams[]` rows | `draft-team-rows` |
| `draft_recap.draft_summary` | `draft-positional-runs` |
| `draft_recap.superlatives` | `draft-superlatives` |

## What is not a stat

Some fields are plumbing: ids, timestamps, the league settings, where the data came from, the schema version and the `narration` blocks. The `narration` block is a trimmed copy of the numbers above, shaped for the narrator and capped in size. It adds no new figures. These fields are listed with a reason in `docs/concepts/not-a-stat.toml`.

## Checking a number yourself

Each stat doc names a committed fixture and shows a slice of its expected output. Run the engine over that fixture and compare, or follow the arithmetic in the doc's example. The code section of every stat doc is the real function, copied verbatim and verified by a test, so the doc and the engine cannot drift apart.
