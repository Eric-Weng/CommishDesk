# Scheduled runs

CommishDesk posts to a league unattended through two GitHub Actions workflows:
`scheduled-draft-recap.yml` (a daily draft-readiness check that builds and posts
the draft recap once Sleeper's draft is complete) and `scheduled-weekly.yml` (a
Wednesday week-resolution check that builds and posts the just-completed week's
recap). Each one computes for itself whether there is anything to do before
`commishdesk` is ever invoked, so a no-work day is a clean skip, never an error.

The reason both carry a healthchecks.io dead-man's-switch: **GitHub disables a
repository's scheduled workflows after 60 days with no repository activity.** A
disabled schedule is silent. No run appears, no check fails, and nothing in the
repository records that the clock stopped — the only symptom is an Issue that
never arrives. The dead-man's-switch turns that silence into a missed ping, which
healthchecks.io alerts on.

Each workflow pings its *own* check (`HEALTHCHECKS_PING_URL` for the daily draft
recap, `COMMISHDESK_WEEKLY_HEALTHCHECKS_PING_URL` for the weekly run), so a
stalled daily schedule cannot mask a stalled weekly one, or the reverse. The two
names are asymmetric — the daily recap's predates this convention (Story 4.7)
and was kept as-is rather than renamed and re-configured; the weekly run's
carries the `COMMISHDESK_WEEKLY_` prefix so the two are never confused for each
other when setting up secrets.

Both workflows ship disarmed: their `schedule:` trigger is commented out and only
`workflow_dispatch` is live until the operator sets the league id variable, the
channel webhook, and — for the weekly run — the `commishdesk-weekly-schedule`
GitHub Environment.

## Confirm or override playoff seeding

At or past the league's playoff start week, the engine derives a playoff bracket
from the standings. The scheduled weekly run can confirm that derived bracket or
replace the seeded order through a repository variable:

* Unset or empty variable: no override; the Issue shows the seeding as
  unconfirmed.
* Value `confirm`: the derived order is confirmed; the unconfirmed note is not
  shown.
* Value `4,2,7,1,9,3` (or any comma-separated roster-id seed order): seeds 1
  through the bracket size are replaced in that order; the remaining rosters
  keep derived order after the cut -- but only from the last regular-season
  week (`playoff_week_start - 1`) onward. A value supplied for an earlier
  week is **ignored**, not applied and not refused: the run ships the normal
  derived picture for that week exactly as if the variable were unset, and
  logs a warning naming the ignored value and the gate week -- visible in the
  triggering GitHub Actions run's log (Actions tab → the workflow run →
  the `commishdesk` step's output). This keeps a
  forgotten, still-set variable from silently reordering standings — and
  turning into a recurring unattended outage — while the regular season is
  still live (epic-5-retro-item-85 / S6).

Set or clear the variable under **Settings → Secrets and variables → Actions →
Variables → Repository variables**:

* `COMMISHDESK_PLAYOFF_SEEDING=confirm`
* `COMMISHDESK_PLAYOFF_SEEDING=4,2,7,1,9,3`

Then re-run the weekly workflow by hand with `workflow_dispatch`. The variable
applies to every scheduled run until it is cleared, so clear it once the
playoffs are over: from the final regular-season week onward, a stale value
still reorders every later week's standings exactly as set — the ignore gate
only protects weeks before that. Do not commit real league ids or real
seeding values anywhere in the repository.
