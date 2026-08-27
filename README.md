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

That command ingests the league's draft, computes the board metrics and grades, writes
the recap with the zero-credential template narrator, and prints it to stdout plus a
local HTML file. No keys. No config. Add an `LLM_API_KEY` later and the same command
produces voiced prose instead.

> **Status:** early build. The MVP (`v0.5`, a draft recap for a single league, Discord
> delivery) is under construction — see [`docs/`](docs/) and the milestone notes. The
> command above is the target of Epic 2; today the package scaffold is landing.

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
| deliver | `commishdesk/deliver/` | Idempotent delivery via a Send Ledger. Each recipient gets each Issue exactly once. |

Everything downstream of `facts/` reads the Facts JSON and nothing else.

## Extending it

Four extension zones, each a documented protocol with exactly one reference
implementation and **no prescribed way to build another** — add a platform adapter, a
voice, a theme, or a stat module without touching the core or asking permission. See
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

[GNU AGPL-3.0](LICENSE). If you run a modified version as a network service, you must
offer its source to your users.

## Legal / attribution

CommishDesk uses the Sleeper API, which is **non-commercial-use only**. The engine sets
an identifying `User-Agent` with a contact URL on every request and links back to
Sleeper from every surface. "Sleeper" is not part of the product name.
