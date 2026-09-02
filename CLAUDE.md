# CLAUDE.md — engine repo rules

This file governs any AI agent (and is a good read for any human) working in the
`commishdesk` repository. The rules below are **not** style preferences. They are the
contract that keeps a distributed, incrementally-built system coherent and keeps this
public repository safe. Treat them as unweakenable: if a task seems to require weakening
one, the task is wrong — stop and raise it, do not soften the rule.

Default branch: **`main`**.

---

## 1. What must NEVER be added to this repository

This is the public, AGPL-3.0 open-core **engine**. It runs with zero credentials. The
following never belong here — not in code, not in tests, not in fixtures, not in docs,
not in git history, not "temporarily":

- **Secrets** — API keys, tokens, webhook URLs, connection strings, signing keys.
  Configuration is via environment variables documented in `.env.example`; the repo
  ships no populated `.env`.
- **Real email addresses** — of contributors, users, leaguemates, or the operator.
  No surface in this product ever displays an email address; no file here contains one.
- **Real league configs** — no `leagues/*.toml`, no Sleeper `league_id`s belonging to
  real leagues, no delivery destinations. The engine takes a league id as a CLI
  argument; it does not carry a list of them.
- **Premium voices** — the public repo contains **exactly one** `Voice` file (the mild
  "beat writer" default). Additional voices are a paid feature and live in the private
  app repo.
- **Auth / accounts / login** — there is no account system anywhere in this product,
  ever. No signup form, no session code, no password/OAuth handling in this repo.
- **Billing / payments** — no Stripe, no payment links, no entitlement state, no price
  logic. Monetization (v3) lives entirely in the private app.
- **Cloudflare (or any hosted-infra) SDKs** — the engine defines a `Store` ABC and
  ships only `FileStore`. `HostedStore` (D1 + R2) is supplied by the app. Engine code
  never imports a Cloudflare SDK.
- **Anything pulled from `../brief/`** — the planning artifacts live in a sibling
  directory and are not part of this repo. Do not copy PRD/spec/epic text into the
  repo. (If a `brief/` directory is ever copied in by mistake, `.gitignore`'s `brief/`
  entry excludes it from being committed — but the rule is "don't copy it in," not
  "the gitignore will catch it.")

If a feature needs any of the above, it belongs in **`../commishdesk-app`** (the
private repo), which is created at **Epic 6** — not before.

---

## 2. Invariants I1–I7 — rules an agent may not weaken

Each invariant has one named test in `tests/test_invariants.py` (`test_I1` … `test_I7`).
CI fails if any invariant test fails. You may not delete a test, loosen its assertion,
mark it `xfail`, or route around it. Until an invariant's epic lands, its test is an
explicit `pytest.skip("pending Epic N")` with a docstring quoting the invariant — that
skip is the only acceptable non-passing state, and it is removed (not weakened) when the
epic implements it.

Per AD-22, every one of these seven gets a real, passing test in *this* engine repo —
not a placeholder that only the app can ever satisfy. Where an invariant's production
code is app-side (claim emails, engagement tracking, mail infrastructure — all Epic 6+),
the engine test proves the invariant's **logic**, provable with fakes over the `Store`
port, and is honest that it does not exercise the real infrastructure. That distinction
is called out per-invariant below.

- **I1 — No paid operation executes for a league with zero verified channels.**
  The Generation Set is *derived* from verified-channel state by exactly one
  constructor. No other code path may add a league to a run. **Engine-testable
  directly:** a test asserts no other write path exists, and that a fake `Store` with
  zero confirmed claims / no validated destination / no budget headroom yields an empty
  set. Mass-registering harvested league ids produces zero work and zero spend.
  Activation requires all three: ≥1 confirmed email claim, an active validated delivery
  destination, and headroom under the activation budget — the *real* claim/destination
  data is app-side, but the constructor's derivation logic lives and is tested here.

- **I2 — An address receives at most one unsolicited message, ever.**
  One pending confirmation per address, globally, for its lifetime; a second request
  for the same address is a silent no-op with a byte-identical response. **Engine-
  testable as logic, not infrastructure:** the idempotency check ("does this address
  already have a pending confirmation") is a pure decision over `Store`-read claim
  state, tested here with a fake store. The actual sending of a confirmation email is
  an app/Epic-6 concern and is not exercised by this test.

- **I3 — LLM cost per league-week is exactly one call.**
  Regardless of member count. The Recap is generated once per league and reused for
  every recipient. The local Layer-4 safety classifier is unpaid and does not count.
  Directly engine-testable against a fake `LLMClient`.

- **I4 — Deterministic output requires no credentials and no paid resources.**
  `ingest → stats → facts → narrate(template) → render` runs with zero credentials and
  no network beyond the Sleeper API. The onboarding sample is stats + templated prose
  only — no LLM. Two runs on one frozen input produce byte-identical output (modulo the
  generated-at timestamp). Directly engine-testable end to end against a fixture.

- **I5 — Continued delivery requires continued engagement.**
  Zero opens and zero clicks for N consecutive weeks → automatic deactivation until
  re-engaged; a deactivated league generates no Issues and no spend; status is computed
  live from engagement history, never stored as a flag. **Engine-testable as logic:**
  the deactivation decision is a pure function over engagement-event data read through
  `Store` (`is_deactivated(events, n_weeks) -> bool`), tested here with fake event
  histories. Real opens/clicks capture is an app/Epic-6 concern (Worker + D1).

- **I6 — Total run cost is computed before any spend.**
  The weekly job derives its complete work list, prices it, then runs fully or not at
  all. Over the configured ceiling → hard abort + operator alert, zero spend. No code
  path discovers an overrun mid-run. Directly engine-testable against a fake priced
  work list.

- **I7 — Transactional and bulk mail use separate sending identities.**
  Confirmations on one subdomain, newsletters on another, so a reputation hit on one
  cannot take down the other. **Engine-testable as a config-shape property, not a real
  send:** the mail adapter interface takes an explicit sending-identity/subdomain
  parameter per mail category and never hardcodes a single domain — a test asserts the
  two categories cannot resolve to the same configured identity. The actual DNS/SPF/
  DKIM/DMARC setup is deployment configuration, verified operationally, not by a test
  in any repo.

`CLAUDE.md` in the private app repo restates these too. They are engine-tested because
self-hosters inherit them and CI enforces them.

---

## 3. Architectural invariants (from the architecture spine)

- **Pipeline isolation (AD-1).** Six stages — `ingest → stats → facts → narrate →
  render → deliver` — each consumes only the immediately-prior stage's output and
  imports nothing from a stage more than one step upstream. The LLM narrator receives
  **only** the sanitized `narration` projection of the Facts JSON — never raw rosters
  or box scores.
- **The Facts JSON is the one published contract (AD-2).** Everything downstream of
  `facts/` reads the Facts JSON and nothing else. Schema is Pydantic v2 in
  `facts/schema.py`; `schema_version` is semver (additive keys → minor, shape change →
  major); consumers ignore unknown keys. The builder validates its own output and fails
  loud; no consumer re-validates.
- **Zero-credential deterministic core (AD-4 / I4).** `narrate/template.py` is built
  **before** `narrate/llm.py`. The LLM narrator and every delivery channel are opt-in
  layers on top.
- **Storage is a port (AD-5).** `Store` ABC + `FileStore` only. No cloud SDK in engine
  code. Every `Store` implementation guarantees read-after-write consistency.
- **One paid LLM call per league-week (AD-8 / I3).** primary → fallback → template.
- **A fault skips one league, never the batch (AD-9).** Typed exceptions under
  `CommishDeskError`, caught per league. No bare `except`.
- **Content safety is enforced in code, not just the prompt (AD-12).** A named-person +
  banned-category hit holds the entire Issue. Safety lists are version-controlled data
  files, editable without a code change.
- **Power ranking (AD-13).** `stats/` emits `model_rank` only; `narrate/` owns
  `published_rank` end to end, bounded to ±`POWER_NUDGE_CAP` (2) with every deviation
  citing a Facts-JSON fact.
- **All league-supplied text is sanitized at one ingest boundary (AD-24).**
  `ingest/sanitize.py` strips control chars, neutralizes URLs, NFKC-normalizes, and
  length-caps `league.name` / `team_name` / `display_name` **before** they enter the
  Facts JSON. Downstream then treats Facts JSON strings as trusted.
- **Extension zones are contracts, not specs (AD-23).** `adapters/`, `voices/`,
  `themes/`, `statmods/` (distinct from the `stats/` pipeline compute package) each carry
  a documented protocol + an eval-fixture location + **at most one** reference
  implementation (the test enforces the ceiling; the reference impls land across
  Epics 2–5). **No code, doc, or comment in this repo prescribes how to implement
  anything behind those protocols.** A test asserts the count behind each protocol never
  exceeds one.

---

## 4. Repo-level safety

- Untrusted issue / PR / comment text is **never** piped into a coding agent with tool
  access. No CI workflow feeds issue or PR bodies into an LLM.
- Contributed fixture files are validated structurally and stripped of unexpected data
  before any model or renderer sees them.
- GitHub Actions: every `uses:` pinned to a full commit SHA; explicit least-privilege
  `permissions:` on every workflow; never `pull_request_target`; no secret exposed to a
  fork-triggered workflow. CI references no repository secrets.

---

## 5. Conventions

- **Python** `>=3.12`; `uv` + committed `uv.lock`. CI on 3.12 and 3.14.
- Sleeper `league_id` / `user_id` / `roster_id` normalized to **strings**.
- **UTC + ISO 8601** everywhere internally; timezone conversion only in `render`.
  Cron expressions in UTC with a documented ET target and a November DST note.
- NFL week = int 1–18; season = int year; money (v3) = integer cents.
- Typed exceptions under `CommishDeskError`, caught per league. No bare `except`.
- stdlib `logging`; JSON lines in a non-TTY, human-readable in a TTY; `--verbose` adds
  DEBUG. Every line carries `league_id` (and `week` where applicable). **Never a secret
  or an email address in a log line.**
- Modules lowercase, one concept each. Facts JSON keys `snake_case`. Pydantic models
  `PascalCase`.
- Every commit is signed off: `git commit -s` (DCO — see `CONTRIBUTING.md`).

---

## 6. ⏸ CHECKPOINT — do not skip Epic 4B

Milestones: **Epics 1–4 are the MVP** (draft-recap v0.5, engine-only). Epic 5 is the
full weekly newsletter (v1). Epic 6 creates `../commishdesk-app`.

**At the end of Epic 4, and again at the end of Epic 5**, the session must **stop and
prompt the operator** — it may not silently continue past this point:

> "You can onboard friends' leagues now via **Epic 4B** — a thin hosting slice (a
> `leagues/` config directory + the weekly cron + an R2 public bucket, ~2–3 days). It
> is not the full app: no self-serve onboarding, no claim pages, no random slugs — you
> paste each webhook yourself. The full **Epic 6** (strangers onboarding themselves)
> waits for 3 consecutive clean weeks and a real second league. Build Epic 4B now, or
> keep going?"

Epic 4B is optional and engine-only, but it must be **offered**, explicitly, at both
checkpoints. Do not treat "keep going" as the default.

---

## 7. Where work goes

| Work | Repo | Milestone |
| --- | --- | --- |
| ingest / stats / facts / narrate / render / deliver / CLI | `commishdesk` (here) | MVP → v1 |
| `FileStore`, extension protocols, fixtures, invariant tests | `commishdesk` (here) | MVP |
| `leagues/` config + weekly cron + R2 public bucket (thin) | `commishdesk` (here) | Epic 4B (optional) |
| Astro site, TS Worker, D1, `HostedStore`, claims, email delivery | `../commishdesk-app` | Epic 6 (v1) |
| Auth, billing, chip-in, personal pages, 2nd platform adapter | `../commishdesk-app` | v3 |

**Engine only. App work goes in `../commishdesk-app`, created at Epic 6.**
