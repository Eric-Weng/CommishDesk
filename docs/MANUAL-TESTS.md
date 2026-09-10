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
- **Last run:** _never_ — **open. Blocks the first real-league draft recap.**

### C2 — `test` as a required status check on `main`

- **Do:** GitHub → repo settings → the `main` ruleset (id `21615482`) → add
  `test` (and decide on `lint` / `test (3.13)` / `test (3.14)`) as required checks.
- **Expect:** a PR can't merge red.
- **Last run:** _partial_ — `test` matrix + `lint` run on every PR; whether they
  are *required* is a settings toggle only Eric can flip. Confirm state.

---

## Change log

- **2026-09-10** — file created. Seeded from the `**Manual checks:**` blocks of
  specs 4.1, 4.2, 4.3, 1b-2, 1-7 and Epic 3 retro item 22. Nothing retired yet.
