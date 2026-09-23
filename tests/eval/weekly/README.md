# tests/eval/weekly/ — the weekly Issue evaluation material

Evaluation material for the weekly Issue's Voice (Story 5.12). See
[`commishdesk/narrate/weekly_template.py`](../../../commishdesk/narrate/weekly_template.py)
for the deterministic template narrator and
[`/docs/EXTENDING.md`](../../../docs/EXTENDING.md) for the `Voice` extension zone.

## What's here

| File | Purpose |
|---|---|
| `week10-sample.md` | A recorded expected-tone weekly Issue in the default "beat writer" Voice, derived from the committed `tests/fixtures/week10-blowout.json` league — the recorded sample the scorer is checked against (not a semantic ground truth). |
| `.gitkeep` | Keeps the directory in git even when a checkout has nothing else here. |

## The rubric

The scorer that grades a weekly Issue against this material lives in
[`tests/test_weekly_voice.py`](../../test_weekly_voice.py) (`_score_weekly_issue`).
Its closed-world half is **not** a heuristic of its own: it calls
`commishdesk.narrate.safety.closed_world_tokens` — the same normalize →
league-name-mask → closed-world pipeline the real safety gate
(`narrate/safety.py`) runs on every Issue. One implementation, called from both
places. It checks three things:

1. **Closed world** (`narrate/safety.closed_world_tokens`). Every numeric token in
   the Issue must be a **member** of the token set built from the narration
   payload (`WeeklyNarration.model_dump_json()`); exact membership, not substring
   containment. A weekly Issue may not cite a number the week's Facts do not
   contain.
2. **Length.** The Issue's character count must be within **±15%** of
   `REFERENCE_TARGET_CHARS`. This half *is* `tests/`-local — the production gate
   has no opinion about length.
3. **Discrete items.** The "Around the League" section must render one discrete
   block per game, so the section reads as a list of results rather than a wall
   of prose. Checked structurally, by re-parsing the sample back into the
   `WeeklyIssue` shape with
   `commishdesk.narrate.weekly_template.weekly_issue_from_text`.

`REFERENCE_TARGET_CHARS = 3800` — the target length the recorded sample and a
live generation are held to (the ±15% band is ≈3,230–4,370 characters). It is a
fixed number here, not a file read, so it does not need to track any source
artifact byte-for-byte. The constant is defined in `tests/test_weekly_voice.py`;
keep the two in sync.

`week10-sample.md` is **derived from the committed week-10 fixture**
(`tests/fixtures/week10-blowout.json`), never from `brief/`. Every proper noun and
number in it is drawn from that week's narration payload.

## Rebuilding the demo narration payload (for a model playground)

To regenerate the exact JSON a model would be handed for the week-10 fixture —
e.g. to paste into a provider playground and eyeball a fresh generation against
this sample — run from the repo root:

```bash
uv run python -c "import json; from pathlib import Path; from commishdesk.ingest import build_league_model, build_week_model, build_player_names, build_player_snapshot; from commishdesk.facts import build_weekly_facts; b=json.loads(Path('tests/fixtures/week10-blowout.json').read_text()); d=build_weekly_facts(build_week_model(b), build_league_model(b), build_player_snapshot(b), build_player_names(b), generated_at='2026-09-07T00:00:00Z', nfl_byes_next_week=frozenset({'IND','NO'})); print(d.narration.model_dump_json(indent=2))"
```

The system prompt the model is paired with is
`commishdesk.voices.load_default_voice("weekly").system_prompt`.
