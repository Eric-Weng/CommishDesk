# `consensus/` — Story 2.4b test fixtures

Both files are **synthetic**. They are keyed to the anonymized Sleeper ids
already in `../rookie-draft.json` / `../week10-superflex.json` (one identical
72-pick board), but every consensus value is made up. The tests assert the
*transform* — fetch → cache → dense re-rank → per-pick delta / per-team extremes —
not any real ranking. See `spec-2-4b-consensus-rank.md` ("Why the values can't
reconcile to the golden").

| File | Shape | Role |
| --- | --- | --- |
| `sleeper-players-nfl.json` | bare JSON object, Sleeper `/players/nfl` wire shape | mock body for the Sleeper-rank path (a rookie draft, Story 9.9) and the demo board; some `search_rank: null` |
| `expected-consensus-metrics.json` | `ConsensusMetrics.model_dump()` + a `_synthetic` note | regression + determinism oracle for `compute_consensus_metrics` over the committed board ranked by that fixture's `search_rank` |

Regenerate `expected-consensus-metrics.json` only when the `stats/consensus.py`
transform changes.
