# V3 milestone 2: dependable local runs

Status: in progress on `v3-runs`. The design keeps the standard-library core and the
dashboard bound to `127.0.0.1`.

## Schedules and triggers

Each JSON flow declares named entries under `schedules`. Exactly one of
`interval_seconds`, `daily`, `cron`, `watch`, or `webhook` selects how an entry fires.
Cron uses five fields in the laptop's local time zone. `catch_up: true` applies only to
time-based schedules: after a restart, one run represents the missed times since the
last recorded due time, capped at 1000 times in the summary. A brand-new schedule
does not catch up before its first run.

A file watch polls a glob relative to the flow folder (or an absolute pattern).
Existing files are ignored on the first poll. A new file triggers once after its size
and modification time remain the same across two polls; deleting and recreating it
triggers again. One poll can pass several new paths in `MINIFLOW_TRIGGER`.

A webhook schedule stores only the name of the environment variable holding its
token. The local endpoint accepts a JSON object and checks `Host`, `Origin` if present,
and a Bearer or `X-MiniFlow-Token` header. A successful call puts its body in
`MINIFLOW_TRIGGER` for that run. The token is never returned by the schedules API.

Schedule discovery reads the schedule declarations without compiling all steps:
the special `MINIFLOW_TRIGGER` value does not exist until a run starts. The child
process still validates the complete flow before execution.

## Queue and history

Scheduled and triggered launches use a FIFO queue with at most 20 waiting runs.
Only one launch from the same named source may wait at a time. A person pressing Run
still gets the existing “still running” response if a run is active. Every queued,
launched, missed, and failed launch is written to `schedule_events`; the dashboard
shows the wait and the current queue. The current controller has **one active run
slot**; configurable parallel run slots remain to be implemented.

At dashboard startup and after each controlled run, retention removes completed
runs beyond the newest 100 per flow. `serve --keep-runs N` and `--keep-days D` select
the count or age policy. Active runs are always preserved. Attempts and task rows
are deleted with their run; old schedule events are capped at 5000 while the newest
event for each named schedule is retained for catch-up. SQLite reuses the freed pages.

`on_failure` already runs a configured block after a failed flow. `on_success` now
runs one after a successful flow and can read step results through placeholders.
The new Slack, Teams, webhook, and email blocks can serve as these actions. Action
errors are reported on stderr, as with the existing failure action.

## Demo and checks

1. Start `miniflow serve --flows DIR --db runs.db --offline` with a short interval
   flow and a slow manual flow. The interval launch shows as queued, then launches
   when the manual run completes.
2. In Editor, add a cron schedule, a file watch, and a webhook schedule. The
   Schedules panel should show each kind without a fake “Next” time for triggers.
3. Add a new CSV to the watched folder, wait until it stops changing, and inspect
   the triggered run's `MINIFLOW_TRIGGER` output. Call the webhook with a wrong
   token (401), then the right one (202) and inspect its run.
4. Run a flow with `on_success`, then one with `on_failure`; inspect their output.
5. Restart the server with `--keep-runs 1` and confirm only the newest completed
   run per flow remains.

Focused checks cover queue ordering, deduplication, capacity, catch-up, file
stability, webhook authentication and HTTP routing, retention, and success actions.
