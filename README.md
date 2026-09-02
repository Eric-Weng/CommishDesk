# CommishDesk

**An automated weekly newspaper for a Sleeper fantasy football league.** Deterministic
statistics computed in code, written up in a warm factual voice, delivered to the group
chat and to inboxes every week, plus an interactive league page. The job is narrow: give
the league something to argue about every week.

The engine is open source (AGPL-3.0) and self-hostable. **It runs with zero
credentials** — no API keys, no account, no network beyond Sleeper's public API.

```bash
uv run commishdesk --league <sleeper_league_id> --draft-recap
```

That command will ingest the league's draft, compute the board metrics and grades,
write the recap with the zero-credential template narrator, and print it to stdout
plus a local HTML file. No keys. No config. Add an `LLM_API_KEY` later and the same
command will produce voiced prose instead.

> **Status:** early build — this is the target shape, not yet the shipped behavior. The
> MVP (`v0.5`, a draft recap for a single league, Discord delivery) is under
> construction — see the milestone notes. The command above is Epic 2's deliverable;
> today the repo carries its governance docs (Epic 1 Story 1.1) with the package
> scaffold landing next.

---

## Quick start (target: under 5 minutes, no keys)

```bash
# 1. clone
git clone https://github.com/Eric-Weng/CommishDesk.git
cd CommishDesk

# 2. install (uv: https://docs.astral.sh/uv/)
uv sync

# 3. run against a committed fixture — no network, no keys
uv run commishdesk --league demo --draft-recap

# 4. run the tests — no network, no keys
uv run pytest -q
```

## What it does

| Stage | Package | Responsibility |
| --- | --- | --- |
| ingest | `commishdesk/ingest/` | Pull a league through a platform-agnostic `Adapter` port (Sleeper is one). Sanitize every league-supplied string at the boundary. |
| stats | `commishdesk/stats/` | Deterministic statistics — all-play, luck / expected wins, coaching efficiency, power-model score, blowout detection, draft grades. No model, no clock, no network. |
| facts | `commishdesk/facts/` | Emit a versioned, self-validated **Facts JSON** — the single contract every narrator and renderer reads. |
| narrate | `commishdesk/narrate/` | Turn the Facts JSON into prose. The **template narrator** (zero credentials, deterministic) is the floor; the **LLM narrator** (one model call, a voice) is an opt-in layer. Content safety runs on both. |
| render | `commishdesk/render/` | One content model, three surfaces — a self-contained interactive web page, a dark-mode-safe email with a plain-text alternative, a Discord post with a rendered image. |
| deliver | `commishdesk/deliver/` | Idempotent delivery via a Send Ledger — each recipient gets each Issue exactly once. **At MVP this is Discord-webhook delivery only.** Email delivery (double opt-in, claims, unsubscribe) is a hosted-service concern that lives in the private app repo from Epic 6 onward, not in this engine. |

Everything downstream of `facts/` reads the Facts JSON and nothing else.

## Extending it

Four extension zones — `adapters/`, `voices/`, `themes/`, `statmods/` — each a documented
protocol with, across the later epics, a single reference implementation each, and **no
prescribed way to build another**: add a platform adapter, a voice, a theme, or a stat
module without touching the core or asking permission. See
[`docs/EXTENDING.md`](docs/EXTENDING.md) and
[`docs/unclaimed-territory.md`](docs/unclaimed-territory.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). The single most useful thing you can send us
is **a league that breaks the optimal-lineup solver** — an anonymized fixture we can add
to the test suite. All commits are signed off (`git commit -s`, DCO).

## The hosted service

A hosted version (self-serve onboarding, email delivery, league pages) is built in a
separate private repo on top of a pinned tag of this engine. This repo has no knowledge
of it and carries none of its concerns — no secrets, no accounts, no billing.

## License

[GNU AGPL-3.0](LICENSE). If you run a modified version of *this engine* as a network
service, you must offer its source to your users. The private hosted app is a separate
codebase that depends on a pinned, published release of this engine rather than
including engine source directly; how that boundary interacts with AGPL §13 has not
been reviewed by a lawyer and isn't a question this README resolves — treat it as open
if the business model ever depends on the answer.

## Legal / attribution

CommishDesk uses the Sleeper API, which is **non-commercial-use only**. The engine sets
an identifying `User-Agent` with a contact URL on every request and links back to
Sleeper from every surface. "Sleeper" is not part of the product name.
