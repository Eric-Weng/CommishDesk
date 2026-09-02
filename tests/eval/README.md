# tests/eval/ — extension-zone evaluation material

This tree is the documented home for evaluation material tied to the four extension zones
(see [`/docs/EXTENDING.md`](../../docs/EXTENDING.md)):

- `tests/eval/adapters/` — recorded / replayable platform responses for `Adapter` work.
- `tests/eval/voices/` — voice eval prompts and expected-tone samples for `Voice` work.
- `tests/eval/themes/` — golden rendered output for `Renderer` work.
- `tests/eval/statmods/` — evaluation inputs and expected values for `StatModule` work.

It is distinct from `tests/fixtures/`, which holds the anonymized league bundles (Story
1.5) that drive the pipeline tests. Only the directories and this README exist now; each
zone's evaluation material lands with that zone's reference implementation.
