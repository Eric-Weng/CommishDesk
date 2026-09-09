# tests/eval/voices/ — `Voice` evaluation material

Evaluation material for the `Voice` extension zone (`commishdesk/voices/`, protocol
in [`__init__.py`](../../../commishdesk/voices/__init__.py), reference impl
`beat_writer.py`). See [`/docs/EXTENDING.md`](../../../docs/EXTENDING.md).

## What's here

| File | Purpose |
|---|---|
| `beat-writer.md` | A recorded expected-tone draft recap in the default "beat writer" voice, derived from the committed demo league — the recorded sample the scorer is checked against (not a semantic ground truth). |
| `.gitkeep` | Keeps the directory in git even when a checkout has nothing else here. |

## The rubric

The scorer that grades a recap against this material lives in
[`tests/test_voices.py`](../../test_voices.py) (`_score_recap`). Its closed-world
half is **not** a heuristic of its own: it calls
`commishdesk.narrate.closed_world_tokens` — the same normalize → league-name-mask
→ closed-world pipeline the real safety gate (`narrate/safety.py`) runs on every
Issue. This module used to carry its own copy, the copy drifted, and the eval
harness started certifying samples the shipping gate would have flagged. One
implementation, called from both places. It checks two things:

1. **Closed world** (`narrate/safety.closed_world_tokens`). Every capitalised or
   numeric token in the recap — minus a curated stop set of scaffolding and
   section-heading words — must be a **member** of the token set built from the
   narration payload (`Narration.model_dump_json()`); exact membership, not
   substring containment, so "202" does not pass on the strength of "2025". A
   letter grade is checked case-folded against the grades actually awarded. A
   recap may not name a player, team, manager, draft slot, grade, or number that
   is not in the supplied facts.
2. **Length.** The recap's character count must be within **±15%** of
   `REFERENCE_TARGET_CHARS`. This half *is* `tests/`-local — the production gate
   has no opinion about length.

`REFERENCE_TARGET_CHARS = 9400` — the raw character length of the body of the
phase-0 draft-recap reference newsletter (`brief/phase-0/`, a planning artifact
that is **not** part of this repo). It is a fixed number here, not a file read:
the ±15% band (≈7,990–10,810 chars) is wide enough that the exact figure does not
need to track the source artifact byte-for-byte. The constant is defined in
`tests/test_voices.py`; keep the two in sync.

`beat-writer.md` is **derived from the committed demo league**
(`tests/fixtures/rookie-draft.json` → `commishdesk --league demo --draft-recap`),
never from `brief/`. Every proper noun and number in it is traceable to that
league's narration payload.

## Rebuilding the demo narration payload (for a model playground)

To regenerate the exact JSON a model would be handed for the demo league — e.g.
to paste into a provider playground and eyeball a fresh generation against this
sample — run from the repo root:

```bash
uv run python -c "from commishdesk import demo; from commishdesk.ingest import build_league_model; from commishdesk.stats import compute_board_metrics, compute_consensus_metrics, compute_draft_grades; from commishdesk.facts import build_draft_recap_facts; m=build_league_model(demo.load_demo_bundle()); b=compute_board_metrics(m); c=compute_consensus_metrics(m, demo.demo_consensus_slots()); g=compute_draft_grades(m, c); d=build_draft_recap_facts(m, b, c, g, generated_at='2026-09-07T00:00:00Z', consensus_source_name=demo.DEMO_CONSENSUS_SOURCE_NAME, consensus_as_of=demo.DEMO_CONSENSUS_AS_OF); print(d.narration.model_dump_json(indent=2))"
```

The system prompt the model is paired with is
`commishdesk.voices.load_default_voice().system_prompt`.
