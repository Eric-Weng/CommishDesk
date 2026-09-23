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
