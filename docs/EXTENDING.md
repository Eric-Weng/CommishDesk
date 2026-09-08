# Extending CommishDesk

CommishDesk has four open extension zones. Each is a Python package under `commishdesk/`
holding exactly one `@runtime_checkable` [`typing.Protocol`][protocol], a documented place
to put evaluation material, and room for **at most one reference implementation** — a rule
enforced by `tests/test_extension_zones.py`, which fails if any zone ever holds two.

Every protocol here is deliberately minimal (one or two members). It fixes only the
*direction* of data flow that the architecture already committed to (AD-1, AD-2, AD-23).
Later epics add members to a protocol when that zone's reference implementation lands; that
is expected and is not a change to this document's contract.

**Nothing in this repository — no code, no comment, no line of this document —
prescribes how to implement anything behind these protocols.** You inherit no base class
and no behavior. Pick your own approach.

For a list of things we would love to see built on these zones — framed as invitations,
not tickets — see [`unclaimed-territory.md`](unclaimed-territory.md).

[protocol]: https://docs.python.org/3/library/typing.html#typing.Protocol

## The four zones

| Zone package | Protocol | Signature | Eval fixtures |
|---|---|---|---|
| `commishdesk/adapters/` | `Adapter` | `fetch(self, league_id: str) -> Mapping[str, Any]` | `tests/eval/adapters/` |
| `commishdesk/voices/` | `Voice` | `system_prompt: str`, `banned_topics: frozenset[str]`, `voice_id: str` | `tests/eval/voices/` |
| `commishdesk/themes/` | `Renderer` | `render(self, facts: FactsJSON) -> str` | `tests/eval/themes/` |
| `commishdesk/statmods/` | `StatModule` | `module_id: str` and `compute(self, facts: FactsJSON) -> Mapping[str, object]` | `tests/eval/statmods/` |

### `Adapter` — `commishdesk/adapters/`

`fetch(self, league_id: str) -> Mapping[str, Any]` returns one league's raw platform data.
`ingest/` sanitizes every league-supplied string at its boundary (AD-24), so an adapter
returns the platform's shape unmodified. Evaluation material — recorded platform responses,
replay fixtures — goes in `tests/eval/adapters/`. The reference `Adapter`, `SleeperAdapter`
in `commishdesk/adapters/sleeper.py`, landed in Epic 2 (Story 2.2).

### `Voice` — `commishdesk/voices/`

A `Voice` supplies `system_prompt: str`, `banned_topics: frozenset[str]`, and
`voice_id: str` — all three members are required. The banned topics merge into the
deterministic content-safety check (`commishdesk/narrate/safety.py`, AD-12) as extracted
keyword patterns: each phrase is lowercased, reduced to its words of four or more letters
(a small stop-set of generic words dropped), and each surviving word is matched
`\bword\b`, case-insensitively, under a synthetic `voice:<voice_id>` category on top of
the base `commishdesk/narrate/safety_lists.toml` lists.

Worked example — a voice with
`banned_topics = {"a manager's politics, religion, or nationality"}` yields the patterns
`\bpolitics\b`, `\breligion\b`, `\bnationality\b` (`"a"`, `"or"` are too short;
`"manager"` is in the stop-set). A recap sentence "he would not stop talking about his
politics" then produces a finding.

**A voice-keyword hit can only ever warn, never hold.** Even in the same sentence as a
manager's name it produces a `banned_topic` (warn) finding, not a `hold_issue` — crude
keyword extraction over free-text prose is too false-positive-prone to silently block a
whole league. Only the curated `[banned_topics]` and `personal_insults` in
`safety_lists.toml` carry the hold tier.

A voice that bans no extra topics uses an empty frozenset. `voice_id` is a stable
identifier for the recap's provenance and (later) voice selection — it has no consumer in
the engine yet. Voice eval prompts and expected-tone samples go in `tests/eval/voices/`;
the length target the sample is scored against (`REFERENCE_TARGET_CHARS`) and the rubric
live in that directory's `README.md`.

The public repo ships **at most one** `Voice` file. That reference implementation —
`commishdesk/voices/beat_writer.py`, the mild "beat writer" default returned by
`commishdesk.voices.load_default_voice()` — landed in Story 3.3 (a test enforces the
ceiling). Additional voices are a paid feature in the private app repo.

**Enabling voiced prose.** The LLM narrator (`commishdesk/narrate/llm.py`) defines an
`LLMClient` protocol (`generate(self, payload: str, voice: Voice) -> str`) with two direct
provider adapters (Anthropic, Google — no aggregator, AD-15). It is opt-in: install the
extra (`pip install 'commishdesk[llm]'`) and export a provider key (`ANTHROPIC_API_KEY` /
`LLM_API_KEY` / `GEMINI_API_KEY` / `GOOGLE_API_KEY`). `commishdesk --draft-recap` then runs
one model call per league through the default voice — `primary → fallback → template`, so a
provider outage silently falls back to the deterministic template recap. `--no-llm` forces
the template narrator even with a key set; `--league demo` is always the template narrator.

### `Renderer` — `commishdesk/themes/`

`render(self, facts: FactsJSON) -> str` turns the Facts JSON into one complete output
surface — a web page, an email, a chat post. The zone directory is `themes/`, but the
protocol is `Renderer` and owns the whole surface. `FactsJSON` is the loose Facts-JSON
alias from `commishdesk.facts` (AD-2); Epic 2 tightens it into the validated `facts.schema`
model. Golden output files go in `tests/eval/themes/`. The reference renderers land in
Epic 4.

### `StatModule` — `commishdesk/statmods/`

A `StatModule` supplies `module_id: str` and
`compute(self, facts: FactsJSON) -> Mapping[str, object]`. Output is additive: keys are
namespaced under `module_id`, and a stat module never touches a prompt or a renderer
(AD-23). The zone is named `statmods/` so it does not shadow the `commishdesk/stats/`
pipeline compute package. Evaluation material goes in `tests/eval/statmods/`. The
reference `StatModule` (playoff odds) lands in v1.

## The one-reference-implementation rule

A "reference implementation" in a zone is a non-underscore-prefixed `.py` module — or a
non-underscore-prefixed subpackage (a subdirectory with its own `__init__.py`) — directly
under the zone package, other than the zone's own `__init__.py`.
`tests/test_extension_zones.py` counts them per zone and asserts the count never exceeds
one. Today `adapters/` holds `SleeperAdapter` (Epic 2) and `voices/` holds the beat-writer
default (Epic 3); `themes/` and `statmods/` still hold zero — those land across Epics 4–5
and v1.

**A second or alternative implementation does not go in this repository.** It lives in the
contributor's own package or fork, or — for a first-party premium implementation — in the
private app repo. The engine discovers it through a configurable set of loader directories
(AD-23), not by adding a file to a zone package here. The one-per-zone ceiling is about
what *ships in this repo*, not about what you are allowed to build.
