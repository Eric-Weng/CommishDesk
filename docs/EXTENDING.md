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
| `commishdesk/voices/` | `Voice` | `system_prompt: str` and `banned_topics: frozenset[str]` | `tests/eval/voices/` |
| `commishdesk/themes/` | `Renderer` | `render(self, facts: FactsJSON) -> str` | `tests/eval/themes/` |
| `commishdesk/statmods/` | `StatModule` | `module_id: str` and `compute(self, facts: FactsJSON) -> Mapping[str, object]` | `tests/eval/statmods/` |

### `Adapter` — `commishdesk/adapters/`

`fetch(self, league_id: str) -> Mapping[str, Any]` returns one league's raw platform data.
`ingest/` sanitizes every league-supplied string at its boundary (AD-24), so an adapter
returns the platform's shape unmodified. Evaluation material — recorded platform responses,
replay fixtures — goes in `tests/eval/adapters/`. The reference `Adapter`, `SleeperAdapter`
in `commishdesk/adapters/sleeper.py`, landed in Epic 2 (Story 2.2).

### `Voice` — `commishdesk/voices/`

A `Voice` supplies both `system_prompt: str` and `banned_topics: frozenset[str]` — the two
members are required. The banned topics merge into the deterministic content-safety check
(AD-12); a voice that bans no extra topics uses an empty frozenset. Voice eval prompts and
expected-tone samples go in `tests/eval/voices/`. The public repo ships **at most one**
`Voice` file — the mild "beat writer" default lands in Story 3.3 (a test enforces the
ceiling); additional voices are a paid feature in the private app repo.

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
one. Today `adapters/` holds its one reference implementation (`SleeperAdapter`, Epic 2);
`voices/`, `themes/`, and `statmods/` still hold zero — the rest land across Epics 3–5.

**A second or alternative implementation does not go in this repository.** It lives in the
contributor's own package or fork, or — for a first-party premium implementation — in the
private app repo. The engine discovers it through a configurable set of loader directories
(AD-23), not by adding a file to a zone package here. The one-per-zone ceiling is about
what *ships in this repo*, not about what you are allowed to build.
