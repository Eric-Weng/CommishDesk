# Contributing to CommishDesk

Thanks for looking. This is a small, deliberately narrow project — an open-source engine
for a weekly fantasy-football newsletter. Contributions are welcome within that scope.

## The most useful thing you can contribute

**Send us a league that breaks the optimal-lineup solver.**

The solver has to find the highest-scoring legal lineup for *any* roster shape — flex
stacks, superflex, IDP, tandem QB, weird taxi/IR rules, orphan rosters, co-owners. Every
real league that trips it is a test case we don't have. Anonymize your league (see
below) and open a PR adding it under `tests/fixtures/` with a one-line note on what
broke.

The same goes for any league that makes the ingest layer, the draft grader, or the
Facts JSON builder raise or produce a wrong number.

## Anonymizing a league

Never commit real display names, real Sleeper `user_id`s, or real avatar URLs. A
contributed fixture must be run through the anonymizer, which strips each section
to an allowlist, replaces member and team names with generated ones, rewrites
every Sleeper account / league / draft id to an opaque token, and drops avatar
hashes and URLs — while preserving `scoring_settings`, `roster_positions`,
`settings`, and matchup / transaction / draft structure verbatim:

```bash
uv run python tools/anonymize.py path/to/your-league-export.json --seed 0 > tests/fixtures/your-case.json
```

`--seed` is optional (default `0`); the same seed gives byte-identical output.
The input must be one JSON object in the bundle shape documented in
[`tests/fixtures/README.md`](tests/fixtures/README.md) — the tool rejects an
unknown top-level key, a missing section, or a wrong-typed section before it
anonymizes anything. Pass `-` (or nothing) to read from stdin.

Any shaping you want — truncating to a range of weeks, dropping failed waiver
claims, changing a roster slot to reproduce a bug — is a manual edit to the raw
bundle *before* you pipe it through the anonymizer; the tool itself only
anonymizes.

A test asserts that no fixture contains a real-looking name, id, or avatar. CI runs
entirely against fixtures — no network, no keys.

## Developer Certificate of Origin (DCO)

Every commit must be signed off. This certifies you wrote the change (or have the right
to submit it) under the project's license — see <https://developercertificate.org/>.

```bash
git commit -s -m "your message"
```

`-s` appends a `Signed-off-by: Your Name <your@email>` line using your git
`user.name` / `user.email`. Set them once:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

PRs with unsigned commits will be asked to amend. To sign off a series you already made:
`git rebase --signoff main`.

## Development setup

```bash
git clone https://github.com/Eric-Weng/CommishDesk.git
cd CommishDesk
uv sync                 # Python >=3.12; installs the dev dependency group
uv run pytest -q        # must pass offline, with no environment variables set
```

- Target Python `>=3.12`. CI runs 3.12 and 3.14.
- The lockfile (`uv.lock`) is committed. If you add a dependency, verify the package
  actually exists and is maintained, then commit the updated lock.

## Ground rules for changes

Read [`CLAUDE.md`](CLAUDE.md) first — it lists what must never enter this repo (secrets,
real emails, real league configs, premium voices, auth, billing) and the invariants
I1–I7 that a change may not weaken. A few consequences for PR authors:

- **The deterministic core stays credential-free.** `ingest → stats → facts →
  narrate(template) → render` must run with no keys and no network beyond Sleeper.
- **Extension zones stay open.** `adapters/`, `voices/`, `themes/`, and `statmods/` (the
  community stat-module zone, kept separate from the `stats/` compute package) get an
  interface and exactly one reference implementation — don't add a second, and don't
  write code that prescribes how someone else's implementation must work. See
  [`docs/EXTENDING.md`](docs/EXTENDING.md).
- **The pipeline flows one way.** A stage consumes only the previous stage's output.
  Nothing downstream of `facts/` reads anything but the Facts JSON.
- **No bare `except`.** Raise a typed exception under `CommishDeskError`.
- **Don't weaken an invariant test.** If your change makes one fail, the change is
  wrong, not the test.

## Reporting security issues

Do **not** open a public issue for a vulnerability. See [`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree your contributions are licensed under the
[GNU AGPL-3.0](LICENSE).
