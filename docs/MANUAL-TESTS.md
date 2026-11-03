# Manual test plan

The checks the automated suite **cannot** cover: things that need a human eye
(dark-mode rendering, "does this read right"), an external service (a live Discord
webhook, an email client, a billed LLM call), or a GitHub-side action.

`uv run pytest -q` is the real safety net. This file is only for what it can't see.

## How this file stays alive

- **Every story** that adds a `**Manual checks:**` block to its spec folds those
  entries into section B or C here when it merges (the `bmad-build` step-05 /
  close does this).
- **Drop an entry** the moment a story makes it moot — an automated test now
  covers it, the feature was removed, or the one-time action is done. Move it to
  the Change log with a one-line reason; don't just delete it.
- **Section A** is the fixed pre-flight; it changes only when the CLI's surface
  changes.
- Record the date and result each time you run a section B/C check, in its
  **Last run** line.

---

## A. Standing smoke test

Run this whole block before any real-league run, before opening a release, or
any time the engine feels off. It is fully offline and deterministic — no
credentials, no network beyond nothing.

```sh
# 1. Full automated suite — the baseline. Expect: NNN passed / 5 skipped.
uv run pytest -q

# 2. Lint + types.
uv run ruff check commishdesk tests
uv run mypy commishdesk

# 3. Demo draft recap, offline. Writes three files into the target dir.
uv run commishdesk --league demo --draft-recap --out-dir /tmp/cd-smoke
ls /tmp/cd-smoke
#   commishdesk-demo-draft-recap.html         <- self-contained web page
#   commishdesk-demo-draft-recap.email.html   <- email-safe <table> HTML
#   commishdesk-demo-draft-recap.txt          <- text/plain alternative

# 4. Run it again into a second dir and diff — output must be byte-identical
#    except the generated-at timestamp line.
uv run commishdesk --league demo --draft-recap --out-dir /tmp/cd-smoke2
diff <(sed 's/[0-9T:.-]\{16,\}Z//' /tmp/cd-smoke/commishdesk-demo-draft-recap.txt) \
     <(sed 's/[0-9T:.-]\{16,\}Z//' /tmp/cd-smoke2/commishdesk-demo-draft-recap.txt)
#   Expect: no output (identical).

# 5. verify-webhook fails clean with no webhook configured.
env -u COMMISHDESK_DISCORD_WEBHOOK_URL uv run commishdesk verify-webhook --league demo
#   Expect: one stderr line naming COMMISHDESK_DISCORD_WEBHOOK_URL, exit 1, no traceback.
```

> On the Windows dev box `uv` is not on PATH — use `uv.exe`. `/tmp` maps through
> Git Bash; any writable dir works.

**Pass** = suite green at the expected counts, lint/types clean, three files
written, the diff is empty, and `verify-webhook` exits 1 with a single line.

---

## B. Human-eye / external-service checks

Each of these needs something the suite can't do. Run the relevant ones after a
change to that surface, and all of them before a release.

### B1 — Web render, dark mode, no overflow

- **Do:** open `commishdesk-demo-draft-recap.html` (and a real-league render if
  you have one) in a browser. Toggle OS dark mode; also try
  `?` → devtools → force `data-theme="dark"` / `"light"` on `<html>`.
- **Expect:** the draft-board grid reads as a grid, colours hold in both themes,
  nothing scrolls horizontally at any window width.
- **Why manual:** visual layout / colour perception.
- **Source:** Story 4.1.
- **Last run:** _never_ — open since 2026-09-10.

### B2 — Email render across clients, dark mode

- **Do:** send `commishdesk-demo-draft-recap.email.html` to yourself; open in
  Gmail (web), Apple Mail, and Outlook (web + desktop if available); toggle dark
  mode in each.
- **Expect:** masthead and the heavy/hair rule pair stay legible after dark-mode
  inversion; no layout collapse; no broken/blocked images (there are none by
  design).
- **Why manual:** each mail client mangles HTML differently; no library models this.
- **Source:** Story 4.2.
- **Last run:** _never_ — **open, flagged in PR #32.**

### B3 — Text alternative reads whole

- **Do:** open `commishdesk-demo-draft-recap.txt`.
- **Expect:** reads as a complete recap — masthead line, all narrated sections,
  `THE BOARD`, `PICKS PER TEAM` — nothing truncated, no leftover `##` markers.
- **Why manual:** "does this read as prose" is a judgement call.
- **Source:** Story 4.2.
- **Last run:** _never_ — open since 2026-09-10.

### B4 — Discord webhook: accept + reject

- **Do:** create a throwaway Discord server + a channel webhook.
  `export COMMISHDESK_DISCORD_WEBHOOK_URL=<that URL>` then
  `uv run commishdesk verify-webhook --league demo`.
  Then edit the var to a deleted/garbled webhook and run it again.
- **Expect:** first run — a visible message in the channel naming the demo
  league, command prints acceptance, exit 0, and **no `@everyone`/role ping fires**
  even if you rename the test channel's league-ish text. Second run — one clear
  stderr line ("Discord rejected the webhook (HTTP 401/404) …"), exit 1, no
  traceback.
- **Why manual:** needs a real Discord endpoint; the suite only has a mock transport.
- **Source:** Story 4.3.
- **Last run:** _never_ — **open, flagged in PR #33.**

### B5 — GitHub CI badge renders

- **Do:** view `README.md` on GitHub.
- **Expect:** the CI badge renders and links to the `test.yml` Actions page.
- **Why manual:** GitHub-rendered Markdown, external image.
- **Source:** Story 1B.2.
- **Last run:** _assumed green_ — re-check on any README or workflow-name change.

---

## C. One-time / gated actions

These are done once (or once per milestone), not on a schedule.

### C1 — Live closed-world false-positive measurement  ·  **BEFORE the first real-league run**

- **Do:** with a provider key set and the `[llm]` extra installed,
  `COMMISHDESK_LIVE_LLM=1 uv run pytest -q tests/test_voices.py::test_live_generation_scores_in_bounds`
  (one billed call). Read the score / `unknown_tokens` it asserts on.
- **Expect:** passes — the model generation scores in bounds against the same
  rubric as the recorded sample. If it fails, the closed-world safety list needs
  another calibration pass before any real league runs.
- **Why gated:** costs money; measures the largest unknown from the Epic 3 retro
  (item 22 / D4).
- **Last run:** Eric, during Epic 3/4 development (exact date untracked — not
  recorded here at the time). Per Eric (2026-09-15): this and equivalent live
  validation work is the reason the closed-world safety list carries as much
  calibration as it does (`narrate/safety_lists.toml`); considered satisfied.
  Not independently re-verified by this session. If the safety list changes
  again, re-run this before the next real-league send.

### C2 — `test` as a required status check on `main`

- **Do:** GitHub → repo settings → the `main` ruleset (id `21615482`) → add
  `test` (and decide on `lint` / `test (3.13)` / `test (3.14)`) as required checks.
- **Expect:** a PR can't merge red.
- **Last run:** _partial_ — `test` matrix + `lint` run on every PR; whether they
  are *required* is a settings toggle only Eric can flip. Confirm state.

### C3 — Scheduled workflow: first live run after configuring vars/secrets

- **Do:** once `vars.COMMISHDESK_LEAGUE_ID` and
  `secrets.{COMMISHDESK_DISCORD_WEBHOOK_URL,ANTHROPIC_API_KEY,GEMINI_API_KEY,
  HEALTHCHECKS_PING_URL}` are set on the repo, uncomment the `schedule:` /
  `cron: '0 23 * * *'` block in `.github/workflows/scheduled-draft-recap.yml`
  (currently disabled — see the `DECIDED 2026-09-14` note in that file) and
  merge. Then either wait for the first scheduled run or fire one manually via
  `workflow_dispatch`.
- **Expect:** the readiness gate correctly no-ops (ping success, no Discord
  post) before the draft completes; once the Sleeper draft's `status` is
  `complete`, the next run posts the recap to Discord, pings
  healthchecks.io success, and the Send Ledger cache entry from that run is
  found by the following run's `restore-keys` prefix match (no duplicate post).
  A forced failure (e.g. a bad webhook) pings the `/fail` healthchecks.io
  endpoint.
- **If it doesn't go as expected:** check the healthchecks.io `/fail` ping
  history first to confirm whether it was a hard failure or a timeout
  (`cancelled()`), read the failing run's logs to find the step, fix the
  underlying cause (bad secret, Sleeper API hiccup, etc.), then re-run via
  `workflow_dispatch` rather than waiting for the next scheduled tick.
- **Why gated:** needs the real repo vars/secrets and a live Discord channel;
  the automated suite only covers the workflow's static shape and content.
- **Source:** Story 4.7 / retro item 64.
- **Last run:** _never_ — **open. Blocked on Eric configuring the repo
  vars/secrets and re-enabling the schedule.**

---

## 9.7 Trade grades in the Issue (browser, Gmail light and dark, Discord)

- **What to do:** run one local weekly build with a graded trade (the demo season's
  week 3 or 6, or the author's league), then view the web page at 320 px and desktop in
  light and dark, send the email to a real Gmail inbox and view it in light and dark, and post
  the Discord text to a test channel.
- **Expected:** one trade card per this-week trade; letter, word and "est." readable without
  colour; sides stack at 320 px; no horizontal scroll; the source line links to League Value and
  nflverse; the email chips keep their contrast in Gmail dark (the open question from the 9.1
  canvas); the Discord post is under 2,000 units with the trade block intact. The locked
  "trade revisited" panel appears on the web page only when rendered with
  `show_trade_revisited=True`.
- **Last run:** _never_ -- **open (needs a real Gmail send and a Discord channel).**

---

## 9.5 Playoff odds in the Issue (browser, Gmail light and dark, Discord)

- **What to do:** run one local weekly build at a mid-season week (the demo season's week 6, or the author's
  league), then view the web page at 320 px and desktop in light and dark, send the email to a real Gmail inbox
  and view it in light and dark, and post the Discord text to a test channel.
- **Expected:** the odds block sits in the Standings card, ranked, with percent text beside each bar; Clinched and
  Eliminated are word pills with no bar; no row reads 0%; Bye and Division columns only where the league has them;
  at 320 px no horizontal scroll and since-last-week sits under a Details toggle; the email shows six rows (four at
  320 px) plus the mover and its two links (the docs link carries the unfilled site marker until 9.8); Gmail dark
  keeps the pill and bar contrast (open question from the 9.1 canvas); the Discord post is under 2,000 units and
  the odds block is the first thing trimmed. A league with more than 12 teams shows the top 8 and "and N more" on
  Discord and in text.
- **Last run:** _never_ -- **open (needs a real Gmail send and a Discord channel).**

---

## 9.2 The facts carry the rest of the regular season (one live run)

- **What to do:** run one weekly Issue for a real league at a mid-season week with the network up
  (`commishdesk --league <sleeper-id> --week <n>`), counting Sleeper requests (for example with
  `HTTPX_LOG_LEVEL=info` or a proxy).
- **Expected:** the Facts carry `matchups.remaining` with weeks `n+1 .. playoff_week_start-1` and
  `complete: true`; the run made `p - n - 2` more Sleeper requests than before (3 at `n = 10`,
  `p = 15`). Record the number in the 9.2 spec's Implementation Notes (input to 9.8).
- **Last run:** _never_ -- **open (needs a real league and network).**

---

## 9.4 Playoff odds timing on the sample container (Docker)

- **What to do:** build the sample image from `commishdesk-app/container/Dockerfile` with the engine from this
  worktree and run the sample build at `--cpus 0.25 --memory 1g`; read the `playoff_odds` step time.
- **Expected:** the block is present for a mid-season week and the whole build stays inside the 60 s sample
  budget; peak memory is well under 200 MB. Record the number in the 9.4 spec's Implementation Notes.
- **Last run:** _never_ -- **open (see the 9.4 Implementation Notes for the local numbers).**

---

## 9.9 Draft recap ranks against League Value (one live run)

- **What to do:** run one draft recap for a real startup or rookie league with the network up
  (nflverse reachable): `commishdesk --league <sleeper-id> --draft-recap`.
- **Expected:** a startup draft names `league_value` in the Facts `consensus_source.name`, reads
  "the League Value board" in the recap and footer, and the footer carries "stats: nflverse,
  CC BY 4.0". A rookie draft names `sleeper_search_rank`, says "Sleeper rank (est.), not a
  stats-based board", and carries no nflverse credit. No reader-facing "consensus" anywhere.
- **Last run:** _never_ -- **open (needs a real league and network).**

---

## Change log

- **2026-09-10** — file created. Seeded from the `**Manual checks:**` blocks of
  specs 4.1, 4.2, 4.3, 1b-2, 1-7 and Epic 3 retro item 22. Nothing retired yet.
