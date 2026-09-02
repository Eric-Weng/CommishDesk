# Unclaimed territory

These are things we would love to see built on top of CommishDesk's extension zones. They
are invitations, not tickets — there is no assignee, no deadline, and no prescribed
design. If one of them speaks to you, take it. Talk to us first only if you want to; the
zones are open by design (see [`EXTENDING.md`](EXTENDING.md)).

- **A second platform adapter.** ESPN, Yahoo, MFL, Fleaflicker — any of them. The
  `Adapter` protocol asks for one league's raw data by id, returned in a shape `ingest/`
  can normalize — Epic 2's Sleeper adapter is the worked example.

- **A stat module for playoff odds.** A Monte Carlo or analytic seeding-probability model
  that reads the Facts JSON and emits per-team odds under `module_id`. Additive only — it
  never edits a prompt or a rendered surface.

- **A stat module for strength of schedule.** Past and remaining, opponent-adjusted or
  raw. Same additive contract.

- **A print-friendly renderer.** A `Renderer` that turns the Facts JSON into a
  single-page PDF or a clean paper layout — the league newspaper as an actual page.

- **A terminal renderer.** A `Renderer` that prints the week to a rich TTY view for
  people who live in their shell.

- **Voice evaluation harness material.** Prompt sets and rubric samples under
  `tests/eval/voices/` that make it easy to judge whether a new voice stays factual and
  on-tone.

- **Adapter response captures.** Anonymized, replayable platform responses under
  `tests/eval/adapters/` so adapter work can be tested fully offline.

- **A localization-aware renderer.** A `Renderer` that emits the same content model in a
  language other than English, pulling only from Facts-JSON values.
